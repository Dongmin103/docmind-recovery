"""Source-exclusive fencing and transactional delivery for host file changes."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from api.db.db_models import (
    DocmindIngestionJob,
    DocmindProject,
    DocmindSource,
    DocmindSourceChangeObservation,
    DocmindSourceChangeReceipt,
    DocmindSourceDocument,
    DocmindSourceSyncSession,
    DocmindSourceVersion,
    Document,
)
from common.docmind_source_path import logical_path_identity_hash, normalize_logical_relative_path

LEASE_SECONDS = 300
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ChangeConflict(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(UTC).replace(tzinfo=None)


def _identity(source_id, worker_id, owner_id):
    for value, limit in ((source_id, 64), (worker_id, 128), (owner_id, 128)):
        if not isinstance(value, str) or len(value) > limit or not _IDENTIFIER.fullmatch(value):
            raise ChangeConflict("REQUEST_INVALID")


def _lock_source(source_id):
    # The source always exists before a session. Locking it also serializes the
    # first acquisition, when no session row exists yet (including on MySQL).
    DocmindSource.update(enabled=DocmindSource.enabled).where(DocmindSource.id == source_id).execute()
    source = DocmindSource.get_or_none(DocmindSource.id == source_id)
    if source is None or not source.enabled:
        raise ChangeConflict("SOURCE_UNAVAILABLE")


def _response(session):
    return {"source_id": session.source_id, "epoch": session.epoch, "last_sequence": session.last_sequence, "lease_expires_at": session.lease_expires_at.isoformat() + "Z"}


def _current(source_id, worker_id, owner_id, epoch, now):
    if not _positive_integer(epoch):
        raise ChangeConflict("REQUEST_INVALID")
    session = DocmindSourceSyncSession.get_or_none(DocmindSourceSyncSession.source_id == source_id)
    if session is None or (session.worker_id, session.owner_id, session.epoch) != (worker_id, owner_id, epoch):
        raise ChangeConflict("SESSION_FENCED")
    if session.lease_expires_at <= now:
        raise ChangeConflict("SESSION_EXPIRED")
    return session


def acquire_session(*, source_id, worker_id, owner_id, now=None):
    _identity(source_id, worker_id, owner_id)
    with DocmindSource._meta.database.atomic():
        _lock_source(source_id)
        now = now or _now()
        session = DocmindSourceSyncSession.get_or_none(DocmindSourceSyncSession.source_id == source_id)
        if session is not None and session.lease_expires_at > now:
            if (session.worker_id, session.owner_id) != (worker_id, owner_id):
                raise ChangeConflict("SESSION_BUSY")
            return _response(session)
        if session is not None and session.epoch >= 9223372036854775807:
            raise ChangeConflict("SESSION_FENCED")
        values = {"worker_id": worker_id, "owner_id": owner_id, "epoch": 1 if session is None else session.epoch + 1, "last_sequence": 0, "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS)}
        if session is None:
            DocmindSourceSyncSession.create(source_id=source_id, **values)
        else:
            DocmindSourceSyncSession.update(**values).where(DocmindSourceSyncSession.source_id == source_id).execute()
        # Return the stored timestamp precision, including MySQL DATETIME rounding.
        return _response(DocmindSourceSyncSession.get_by_id(source_id))


def renew_session(*, source_id, worker_id, owner_id, epoch, now=None):
    _identity(source_id, worker_id, owner_id)
    with DocmindSource._meta.database.atomic():
        _lock_source(source_id)
        now = now or _now()
        session = _current(source_id, worker_id, owner_id, epoch, now)
        session.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        session.save(only=[DocmindSourceSyncSession.lease_expires_at])
        return _response(DocmindSourceSyncSession.get_by_id(source_id))


def commit_request(*, source_id, worker_id, owner_id, epoch, sequence, request_id, payload, apply, now=None):
    """Run DB-only apply under the source lock. External search cleanup runs later.

    Receipt expiry cannot permit a replay: the session high-watermark is stored
    separately and survives both receipt pruning and process restarts.
    """
    _identity(source_id, worker_id, owner_id)
    if not _positive_integer(sequence) or not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
        raise ChangeConflict("REQUEST_INVALID")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    payload_hash = hashlib.sha256(encoded).hexdigest()
    with DocmindSource._meta.database.atomic():
        _lock_source(source_id)
        now = now or _now()
        session = _current(source_id, worker_id, owner_id, epoch, now)
        receipt = DocmindSourceChangeReceipt.get_or_none(
            (DocmindSourceChangeReceipt.source_id == source_id) & (DocmindSourceChangeReceipt.epoch == epoch) & (DocmindSourceChangeReceipt.request_id == request_id)
        )
        if receipt is not None:
            if receipt.payload_sha256 != payload_hash or receipt.sequence != sequence:
                raise ChangeConflict("REQUEST_CONFLICT")
            return json.loads(receipt.result_json)
        if sequence <= session.last_sequence:
            raise ChangeConflict("SEQUENCE_REPLAY")
        if sequence != session.last_sequence + 1:
            raise ChangeConflict("SEQUENCE_GAP")
        result = apply()
        DocmindSourceChangeReceipt.create(
            id=uuid.uuid4().hex,
            source_id=source_id,
            epoch=epoch,
            sequence=sequence,
            request_id=request_id,
            payload_sha256=payload_hash,
            result_json=json.dumps(result, allow_nan=False),
            received_at=now,
        )
        session.last_sequence = sequence
        session.save(only=[DocmindSourceSyncSession.last_sequence])
        return result


def _positive_integer(value):
    return type(value) is int and 0 < value <= 9223372036854775807


def _validate_change(value):
    if not isinstance(value, dict):
        raise ChangeConflict("REQUEST_INVALID")
    item = dict(value)
    required = {"kind", "relative_path", "generation", "observation_id"}
    kind = item.get("kind")
    optional = {"host_file_id"}
    if kind in {"upsert", "move"}:
        required |= {"ciphertext_sha256", "size", "mtime_ns"}
    if kind == "move":
        required |= {"old_relative_path", "host_file_id", "old_absence_confirmed", "root_access_confirmed"}
    if kind == "delete":
        required |= {"absence_confirmed", "root_access_confirmed"}
    if kind not in {"dirty", "upsert", "move", "delete"} or not required <= item.keys() or item.keys() - required - optional:
        raise ChangeConflict("REQUEST_INVALID")
    if not _positive_integer(item["generation"]) or not isinstance(item["observation_id"], str) or not _IDENTIFIER.fullmatch(item["observation_id"]):
        raise ChangeConflict("REQUEST_INVALID")
    try:
        for key in ("relative_path", "old_relative_path"):
            if key in item:
                if not isinstance(item[key], str):
                    raise ValueError()
                item[key] = normalize_logical_relative_path(item[key])
    except ValueError as error:
        raise ChangeConflict("REQUEST_INVALID") from error
    for key in ("root_access_confirmed", "absence_confirmed", "old_absence_confirmed"):
        if key in item and item[key] is not True:
            raise ChangeConflict("REQUEST_INVALID")
    if "host_file_id" in item and (not isinstance(item["host_file_id"], str) or not _IDENTIFIER.fullmatch(item["host_file_id"])):
        raise ChangeConflict("REQUEST_INVALID")
    if kind in {"upsert", "move"} and (
        not isinstance(item["ciphertext_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", item["ciphertext_sha256"])
        or any(type(item[k]) is not int or not 0 <= item[k] <= 9223372036854775807 for k in ("size", "mtime_ns"))
    ):
        raise ChangeConflict("REQUEST_INVALID")
    return item


def _save_mapping(mapping, **values):
    DocmindSourceDocument.update(**values).where(DocmindSourceDocument.id == mapping.id).execute()
    for key, value in values.items():
        setattr(mapping, key, value)


def _begin_generation(mapping, epoch, generation):
    if (mapping.observation_epoch, mapping.observation_generation) == (epoch, generation):
        return
    _save_mapping(
        mapping,
        source_dirty=True,
        observation_epoch=epoch,
        observation_generation=generation,
        stable_observation_count=0,
        stable_observed_at=None,
        latest_target_version_id=None,
        generation=mapping.generation + 1,
    )
    DocmindIngestionJob.update(lifecycle_state="SUPERSEDED", retry_not_before=None).where(
        (DocmindIngestionJob.source_document_id == mapping.id) & (DocmindIngestionJob.lifecycle_state.in_(["DISCOVERED", "RETRY_WAIT"]))
    ).execute()


def _upsert_observation(mapping, item, project, active, now):
    from api.apps.services import docmind_ingestion_service as ingestion

    same = (mapping.observed_ciphertext_sha256, mapping.observed_size, mapping.observed_mtime_ns) == (item["ciphertext_sha256"], item["size"], item["mtime_ns"])
    if not same:
        _save_mapping(mapping, source_dirty=True, latest_target_version_id=None)
    if same and mapping.stable_observation_count >= 2 and not mapping.source_dirty:
        return {"state": "INDEX_CURRENT", "version_id": mapping.latest_target_version_id}
    if same and mapping.stable_observation_count and mapping.stable_observed_at and now < mapping.stable_observed_at + timedelta(seconds=10):
        return {"state": "WAITING_SOURCE_STABLE", "stable_observations": mapping.stable_observation_count}
    count = mapping.stable_observation_count + 1 if same else 1
    if active is not None and active.lifecycle_state == "ACTIVE" and active.search_cleanup_complete and (active.ciphertext_sha256, active.ciphertext_size) == (item["ciphertext_sha256"], item["size"]):
        _save_mapping(mapping, observed_ciphertext_sha256=item["ciphertext_sha256"], observed_size=item["size"], observed_mtime_ns=item["mtime_ns"], stable_observation_count=min(2, count))
        result = {"state": "INDEX_REUSED" if count >= 2 else "WAITING_SOURCE_STABLE", "version_id": active.id}
    else:
        result = ingestion._observe_mapped_source_version(project, mapping, ciphertext_sha256=item["ciphertext_sha256"], ciphertext_size=item["size"], source_mtime_ns=item["mtime_ns"])
        # Keep the batch's in-memory mapping current if another item addresses it.
        mapping.observed_ciphertext_sha256, mapping.observed_size, mapping.observed_mtime_ns = item["ciphertext_sha256"], item["size"], item["mtime_ns"]
        mapping.stable_observation_count = count
    _save_mapping(mapping, stable_observed_at=now)
    if count >= 2 and result["state"] != "DEFERRED_PREVIOUS_CLEANUP":
        _save_mapping(mapping, source_dirty=False, latest_target_version_id=result["version_id"])
    return result


def _apply_changes(request, items, now):
    from api.apps.services import docmind_reconciliation_service as reconciliation

    source = DocmindSource.get_by_id(request["source_id"])
    project = DocmindProject.get_by_id(source.project_id)
    hashes = {logical_path_identity_hash(item[key]) for item in items for key in ("relative_path", "old_relative_path") if key in item}
    mappings = {
        row.relative_path_hash: row
        for row in DocmindSourceDocument.select().where(
            (DocmindSourceDocument.project_id == project.id) & (DocmindSourceDocument.source_id == source.id) & DocmindSourceDocument.relative_path_hash.in_(hashes)
        )
    }
    active_ids = {row.active_source_version_id for row in mappings.values() if row.active_source_version_id}
    active = {row.id: row for row in DocmindSourceVersion.select().where(DocmindSourceVersion.id.in_(active_ids))} if active_ids else {}
    observation_ids = [item["observation_id"] for item in items]
    observed = {
        row.observation_id: row
        for row in DocmindSourceChangeObservation.select().where(
            (DocmindSourceChangeObservation.source_id == source.id) & (DocmindSourceChangeObservation.epoch == request["epoch"]) & DocmindSourceChangeObservation.observation_id.in_(observation_ids)
        )
    }
    results = []
    for item in items:
        digest = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        prior = observed.get(item["observation_id"])
        if prior:
            if prior.payload_sha256 != digest:
                raise ChangeConflict("OBSERVATION_CONFLICT")
            results.append(json.loads(prior.result_json))
            continue
        path_hash = logical_path_identity_hash(item["relative_path"])
        mapping = mappings.get(path_hash)
        state = None
        if item["kind"] == "move":
            old_hash = logical_path_identity_hash(item["old_relative_path"])
            previous = mappings.get(old_hash)
            identity = request["worker_id"] + ":" + item["host_file_id"]
            if previous is None or previous.deleted_at is not None or previous.host_file_identity != identity or (mapping is not None and mapping.id != previous.id):
                state = {"state": "DEFERRED", "reason": "MOVE_IDENTITY_UNCONFIRMED"}
            elif (previous.observation_epoch, previous.observation_generation) > (request["epoch"], item["generation"]):
                state = {"state": "IGNORED_STALE"}
            else:
                mapping = previous
                _save_mapping(mapping, relative_path=item["relative_path"], relative_path_hash=path_hash)
                Document.update(name=item["relative_path"].rsplit("/", 1)[-1]).where(Document.id == mapping.document_id).execute()
                mappings.pop(old_hash, None)
                mappings[path_hash] = mapping
        if state is None and mapping is None and item["kind"] == "upsert":
            suffix = item["relative_path"].rsplit(".", 1)[-1].lower()
            if suffix not in {"pdf", "doc", "docx", "xls", "xlsx", "pptx", "hwp", "hwpx"}:
                state = {"state": "DEFERRED", "reason": "UNSUPPORTED_FORMAT"}
            elif not source.default_folder_id:
                state = {"state": "DEFERRED", "reason": "DEFAULT_FOLDER_UNCONFIGURED"}
            else:
                mapping = reconciliation._provision_discovered_document(source=source, project=project, relative_path=item["relative_path"])
                mappings[path_hash] = mapping
        if state is None and mapping is None:
            state = {"state": "UNREGISTERED"}
        if state is None:
            prior_generation = (mapping.observation_epoch, mapping.observation_generation)
            incoming_generation = (request["epoch"], item["generation"])
            if prior_generation > incoming_generation or (mapping.deleted_at is not None and (item["kind"] != "upsert" or incoming_generation <= prior_generation)):
                state = {"state": "IGNORED_STALE"}
            else:
                if mapping.deleted_at is not None:
                    reconciliation.revive_authoritatively_deleted_mapping(mapping, now=now)
                _begin_generation(mapping, request["epoch"], item["generation"])
                _save_mapping(mapping, last_change_sequence=request["sequence"], last_change_received_at=now)
                if item["kind"] == "dirty":
                    state = {"state": "DIRTY"}
                elif item["kind"] in {"upsert", "move"}:
                    if "host_file_id" in item:
                        _save_mapping(mapping, host_file_identity=request["worker_id"] + ":" + item["host_file_id"])
                    state = _upsert_observation(mapping, item, project, active.get(mapping.active_source_version_id), now)
                else:
                    reconciliation.stage_authoritative_deletions([mapping], authority_kind="INCREMENTAL_ABSENCE", authority_scan_id=None, now=now)
                    state = {"state": "DELETION_PENDING"}
        result = {"observation_id": item["observation_id"], "relative_path": item["relative_path"], **state}
        if mapping is not None:
            result["document_id"] = mapping.document_id
        row = DocmindSourceChangeObservation.create(
            id=hashlib.sha256(f"{source.id}\x1f{request['epoch']}\x1f{item['observation_id']}".encode()).hexdigest(),
            source_id=source.id,
            epoch=request["epoch"],
            observation_id=item["observation_id"],
            payload_sha256=digest,
            result_json=json.dumps(result),
            received_at=now,
        )
        observed[item["observation_id"]] = row
        results.append(result)
    return {"accepted": True, "epoch": request["epoch"], "sequence": request["sequence"], "items": results}


def receive_changes(request, *, now=None, retention_adapter=None):
    required = {"protocol_version", "source_id", "worker_id", "owner_id", "epoch", "sequence", "request_id", "items"}
    if not isinstance(request, dict) or set(request) != required or type(request["protocol_version"]) is not int or request["protocol_version"] != 2:
        raise ChangeConflict("REQUEST_INVALID")
    if not isinstance(request["items"], list) or not 1 <= len(request["items"]) <= 250:
        raise ChangeConflict("REQUEST_INVALID")
    try:
        if len(json.dumps(request, ensure_ascii=False, allow_nan=False).encode()) > 1024 * 1024:
            raise ChangeConflict("REQUEST_INVALID")
    except (ValueError, TypeError) as error:
        raise ChangeConflict("REQUEST_INVALID") from error
    _identity(request.get("source_id"), request.get("worker_id"), request.get("owner_id"))
    items = [_validate_change(item) for item in request["items"]]
    if any("host_file_id" in item and len(request.get("worker_id", "")) + 1 + len(item["host_file_id"]) > 255 for item in items):
        raise ChangeConflict("REQUEST_INVALID")
    args = {key: request[key] for key in ("source_id", "worker_id", "owner_id", "epoch", "sequence", "request_id")}
    result = commit_request(**args, payload=request, now=now, apply=lambda: _apply_changes(request, items, now or _now()))
    deleted_ids = [item["document_id"] for item in result["items"] if item["state"] == "DELETION_PENDING"]
    if deleted_ids:
        from api.apps.services import docmind_reconciliation_service as reconciliation

        mappings = list(
            DocmindSourceDocument.select().where(
                (DocmindSourceDocument.source_id == request["source_id"]) & DocmindSourceDocument.document_id.in_(deleted_ids) & DocmindSourceDocument.deleted_at.is_null(False)
            )
        )
        reconciliation.finish_authoritative_deletions(
            mappings, authority_kind="INCREMENTAL_ABSENCE", authority_scan_id=None, adapter=retention_adapter or reconciliation.ProductionSearchRetentionAdapter(), now=now or _now()
        )
    return result
