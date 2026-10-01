"""Source-exclusive fencing and transactional delivery for host file changes."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from api.db.db_models import DocmindSource, DocmindSourceChangeReceipt, DocmindSourceSyncSession

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
    if type(epoch) is not int or epoch < 1:
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
        values = {"worker_id": worker_id, "owner_id": owner_id, "epoch": 1 if session is None else session.epoch + 1, "last_sequence": 0, "lease_expires_at": now + timedelta(seconds=LEASE_SECONDS)}
        if session is None:
            session = DocmindSourceSyncSession.create(source_id=source_id, **values)
        else:
            DocmindSourceSyncSession.update(**values).where(DocmindSourceSyncSession.source_id == source_id).execute()
            session = DocmindSourceSyncSession.get_by_id(source_id)
        return _response(session)


def renew_session(*, source_id, worker_id, owner_id, epoch, now=None):
    _identity(source_id, worker_id, owner_id)
    with DocmindSource._meta.database.atomic():
        _lock_source(source_id)
        now = now or _now()
        session = _current(source_id, worker_id, owner_id, epoch, now)
        session.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
        session.save(only=[DocmindSourceSyncSession.lease_expires_at])
        return _response(session)


def commit_request(*, source_id, worker_id, owner_id, epoch, sequence, request_id, payload, apply, now=None):
    """Run DB-only apply under the source lock. External search cleanup runs later.

    Receipt expiry cannot permit a replay: the session high-watermark is stored
    separately and survives both receipt pruning and process restarts.
    """
    _identity(source_id, worker_id, owner_id)
    if type(sequence) is not int or sequence < 1 or not isinstance(request_id, str) or not _IDENTIFIER.fullmatch(request_id):
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
