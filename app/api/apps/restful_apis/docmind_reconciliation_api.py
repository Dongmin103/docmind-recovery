from __future__ import annotations

import logging
from datetime import UTC, datetime

from quart import Response, request

from api.apps import login_required
from api.apps.services import docmind_reconciliation_service, docmind_worker_auth
from api.utils.api_utils import add_tenant_id_to_kwargs, get_error_data_result, get_result

logger = logging.getLogger(__name__)


def _path_and_query() -> str:
    query = request.query_string.decode("ascii")
    return request.path + (f"?{query}" if query else "")


async def _authenticate(body: bytes) -> str:
    key_id = str(request.headers.get("X-DocMind-Key-Id") or "")
    docmind_worker_auth.verify(
        method=request.method,
        path_and_query=_path_and_query(),
        body=body,
        key_id=key_id,
        timestamp=str(request.headers.get("X-DocMind-Timestamp") or ""),
        nonce=str(request.headers.get("X-DocMind-Nonce") or ""),
        content_sha256=str(request.headers.get("X-DocMind-Content-SHA256") or "").lower(),
        signature=str(request.headers.get("X-DocMind-Signature") or "").lower(),
    )
    return key_id


def _signed(payload: dict, key_id: str, status: int = 200) -> Response:
    body, headers = docmind_worker_auth.signed_json_body(
        payload,
        method=request.method,
        path_and_query=_path_and_query(),
        key_id=key_id,
    )
    return Response(body, status=status, content_type="application/json", headers=headers)


def _occurred_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise docmind_reconciliation_service.DocmindReconciliationError(
            "DOCMIND_RECONCILIATION_TIMESTAMP_INVALID"
    )
    try:
        normalized = value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise docmind_reconciliation_service.DocmindReconciliationError(
            "DOCMIND_RECONCILIATION_TIMESTAMP_INVALID"
        ) from error
    if parsed.tzinfo is None:
        raise docmind_reconciliation_service.DocmindReconciliationError(
            "DOCMIND_RECONCILIATION_TIMESTAMP_INVALID"
        )
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _base(req: object) -> dict:
    if not isinstance(req, dict) or req.get("protocol_version") not in {1, 2}:
        raise docmind_reconciliation_service.DocmindReconciliationError(
            "DOCMIND_RECONCILIATION_REQUEST_INVALID"
        )
    return req


@manager.route("/cloud-sync/host-worker/scans/events", methods=["POST"])  # noqa: F821
async def source_scan_event():
    body = await request.get_data()
    key_id = ""
    try:
        key_id = await _authenticate(body)
        req = _base(await request.get_json(silent=True))
        event = req.get("event")
        common = {"protocol_version", "worker_id", "source_id", "scan_id", "event", "occurred_at"}
        v2 = req["protocol_version"] == 2
        if v2:
            common |= {"owner_id", "epoch"}
        fence = {"owner_id": req.get("owner_id"), "epoch": req.get("epoch")} if v2 else {}
        _occurred_at(req.get("occurred_at"))
        if event == "started":
            expected = common | {"reason", "root_access_confirmed"}
            if req.get("reason") == "scheduled":
                expected.add("schedule_fencing_token")
            if set(req) != expected:
                raise docmind_reconciliation_service.DocmindReconciliationError(
                    "DOCMIND_RECONCILIATION_REQUEST_INVALID"
                )
            if req.get("reason") not in ({"startup", "restart", "watcher_error", "scheduled"} if v2 else {"startup", "scheduled", "manual"}):
                raise docmind_reconciliation_service.DocmindReconciliationError(
                    "DOCMIND_RECONCILIATION_REQUEST_INVALID"
                )
            result = docmind_reconciliation_service.begin_scan(
                source_id=req["source_id"],
                scan_id=req["scan_id"],
                worker_id=req["worker_id"],
                root_access_confirmed=req.get("root_access_confirmed") is True,
                trigger=f"HOST_{str(req['reason']).upper()}",
                schedule_fencing_token=req.get("schedule_fencing_token"),
                protocol_version=req["protocol_version"], **fence,
            )
        elif event == "batch":
            if set(req) != common | {"batch_index", "documents"}:
                raise docmind_reconciliation_service.DocmindReconciliationError(
                    "DOCMIND_RECONCILIATION_REQUEST_INVALID"
                )
            result = docmind_reconciliation_service.record_scan_batch(
                source_id=req["source_id"],
                scan_id=req["scan_id"],
                worker_id=req["worker_id"],
                batch_index=req["batch_index"],
                documents=req["documents"],
                **fence,
            )
        elif event == "completed":
            if set(req) != common | {"complete", "file_count", "batch_count"}:
                raise docmind_reconciliation_service.DocmindReconciliationError(
                    "DOCMIND_RECONCILIATION_REQUEST_INVALID"
                )
            result = docmind_reconciliation_service.complete_scan(
                source_id=req["source_id"],
                scan_id=req["scan_id"],
                worker_id=req["worker_id"],
                complete=req.get("complete") is True,
                file_count=req["file_count"],
                batch_count=req["batch_count"],
                **fence,
            )
        elif event == "failed":
            expected = common | {
                "complete",
                "error_code",
                "file_count",
                "batch_count",
                "root_access_confirmed",
            }
            if "schedule_fencing_token" in req:
                expected.add("schedule_fencing_token")
            if set(req) != expected or req.get("complete") is not False:
                raise docmind_reconciliation_service.DocmindReconciliationError(
                    "DOCMIND_RECONCILIATION_REQUEST_INVALID"
                )
            result = docmind_reconciliation_service.fail_scan(
                source_id=req["source_id"],
                scan_id=req["scan_id"],
                worker_id=req["worker_id"],
                error_code=req["error_code"],
                root_access_confirmed=req.get("root_access_confirmed") is True,
                schedule_fencing_token=req.get("schedule_fencing_token"),
                protocol_version=req["protocol_version"],
                **fence,
            )
        else:
            raise docmind_reconciliation_service.DocmindReconciliationError(
                "DOCMIND_RECONCILIATION_EVENT_INVALID"
            )
        return _signed(result, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_reconciliation_service.DocmindReconciliationError as error:
        return _signed({"error": error.code}, key_id, 409)
    except Exception:
        logger.exception("DocMind source scan event failed")
        if key_id:
            return _signed({"error": "DOCMIND_RECONCILIATION_INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")


@manager.route("/cloud-sync/host-worker/scans/candidates", methods=["POST"])  # noqa: F821
async def source_scan_candidates():
    body = await request.get_data()
    key_id = ""
    try:
        key_id = await _authenticate(body)
        req = _base(await request.get_json(silent=True))
        expected = {"protocol_version", "worker_id", "owner_id", "epoch", "source_id", "scan_id", "after_document_id", "limit"}
        if req["protocol_version"] != 2 or set(req) != expected:
            raise docmind_reconciliation_service.DocmindReconciliationError("DOCMIND_RECONCILIATION_REQUEST_INVALID")
        result = docmind_reconciliation_service.missing_scan_candidates(
            source_id=req["source_id"], scan_id=req["scan_id"], worker_id=req["worker_id"],
            owner_id=req["owner_id"], epoch=req["epoch"],
            after_document_id=req["after_document_id"], limit=req["limit"],
        )
        return _signed(result, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_reconciliation_service.DocmindReconciliationError as error:
        return _signed({"error": error.code}, key_id, 409)
    except Exception:
        logger.exception("DocMind scan candidate page failed")
        if key_id:
            return _signed({"error": "DOCMIND_RECONCILIATION_INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
@manager.route("/cloud-sync/host-worker/deletions", methods=["POST"])  # noqa: F821
async def source_deletion_event():
    body = await request.get_data()
    key_id = ""
    try:
        key_id = await _authenticate(body)
        req = _base(await request.get_json(silent=True))
        if req["protocol_version"] != 1:
            raise docmind_reconciliation_service.DocmindReconciliationError(
                "DOCMIND_RECONCILIATION_PROTOCOL_OUTDATED"
            )
        expected = {
            "protocol_version",
            "worker_id",
            "source_id",
            "document_id",
            "relative_path",
            "observed_at",
            "root_access_confirmed",
            "absence_confirmed",
        }
        if set(req) != expected:
            raise docmind_reconciliation_service.DocmindReconciliationError(
                "DOCMIND_RECONCILIATION_REQUEST_INVALID"
            )
        _occurred_at(req.get("observed_at"))
        result = docmind_reconciliation_service.confirm_event_deletion(
            source_id=req["source_id"],
            document_id=req["document_id"],
            relative_path=req["relative_path"],
            root_access_confirmed=req.get("root_access_confirmed") is True,
            absence_confirmed=req.get("absence_confirmed") is True,
            # Validate the worker timestamp for the signed event, but use the
            # server clock as retention/deletion authority.
            observed_at=None,
        )
        return _signed(result, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_reconciliation_service.DocmindReconciliationError as error:
        return _signed({"error": error.code}, key_id, 409)
    except Exception:
        logger.exception("DocMind source deletion event failed")
        if key_id:
            return _signed({"error": "DOCMIND_RECONCILIATION_INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")


@manager.route("/cloud-sync/host-worker/reconciliation/claim", methods=["POST"])  # noqa: F821
async def claim_reconciliation():
    body = await request.get_data()
    key_id = ""
    try:
        key_id = await _authenticate(body)
        req = _base(await request.get_json(silent=True))
        if set(req) - {"protocol_version", "worker_id", "lease_seconds", "source_id"}:
            raise docmind_reconciliation_service.DocmindReconciliationError(
                "DOCMIND_RECONCILIATION_REQUEST_INVALID"
            )
        worker_id = str(req.get("worker_id") or "")
        lease_seconds = req.get("lease_seconds", 300)
        source_id = req.get("source_id")
        if not isinstance(lease_seconds, int) or (
            "source_id" in req and (not isinstance(source_id, str) or not source_id)
        ):
            raise docmind_reconciliation_service.DocmindReconciliationError(
                "DOCMIND_RECONCILIATION_REQUEST_INVALID"
            )
        retries = docmind_reconciliation_service.reschedule_retryable_jobs(
            source_id=source_id
        )
        exclusions_finished = 0
        exclusions_retry_failed = False
        try:
            exclusions_finished = docmind_reconciliation_service.retry_pending_authoritative_deletions(
                source_id=source_id, limit=100
            )
        except Exception:
            logger.exception("DocMind pending search exclusion retry failed")
            exclusions_retry_failed = True
        scan = docmind_reconciliation_service.claim_due_midnight_scan(
            worker_id, lease_seconds=lease_seconds, source_id=source_id
        )
        return _signed({"scan": scan, "retries_scheduled": len(retries),
                        "exclusions_finished": exclusions_finished,
                        "exclusions_retry_failed": exclusions_retry_failed}, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_reconciliation_service.DocmindReconciliationError as error:
        return _signed({"error": error.code}, key_id, 409)
    except Exception:
        logger.exception("DocMind reconciliation claim failed")
        if key_id:
            return _signed({"error": "DOCMIND_RECONCILIATION_INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")


@manager.route("/docmind/source-mappings/dry-run", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def source_mapping_dry_run(tenant_id: str):
    try:
        req = await request.get_json(silent=True)
        if not isinstance(req, dict) or set(req) != {"candidates"}:
            return get_error_data_result(message="DOCMIND_MAPPING_DRY_RUN_INVALID"), 400
        return get_result(
            data=docmind_reconciliation_service.mapping_dry_run(tenant_id, req["candidates"])
        )
    except docmind_reconciliation_service.DocmindReconciliationError as error:
        return get_error_data_result(message=error.code), 400
    except Exception:
        logger.exception("DocMind source mapping dry-run failed")
        return get_error_data_result(message="DOCMIND_MAPPING_DRY_RUN_INTERNAL_ERROR"), 500
