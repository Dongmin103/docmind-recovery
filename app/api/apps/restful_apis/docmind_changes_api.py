"""Signed host protocol for file-level synchronization."""

from __future__ import annotations

import asyncio
import json
import logging

from quart import Response, request

from api.apps.services import docmind_change_service, docmind_worker_auth

logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 1024 * 1024


@manager.route("/cloud-sync/host-worker/changes", methods=["POST"])  # noqa: F821
async def change_batch():
    key_id = None
    try:
        key_id, failure, body = await _authenticate()
        if failure is not None:
            return failure
        result = docmind_change_service.receive_changes(_json_body(body))
        return _signed(result, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_change_service.ChangeConflict as error:
        return _signed({"error": error.code}, key_id, 400 if error.code == "REQUEST_INVALID" else 409)
    except Exception:
        logger.exception("DocMind incremental changes failed")
        if key_id:
            return _signed({"error": "INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")


def _path_and_query():
    query = request.query_string.decode("ascii")
    return request.path + ("?" + query if query else "")


def _signed(payload, key_id, status=200):
    body, headers = docmind_worker_auth.signed_json_body(payload, method=request.method, path_and_query=_path_and_query(), key_id=key_id)
    return Response(body, status=status, content_type="application/json", headers=headers)


def _json_body(body):
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise docmind_change_service.ChangeConflict("REQUEST_INVALID") from error


async def _authenticate():
    if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
        return None, Response(b'{"error":"REQUEST_TOO_LARGE"}', status=413, content_type="application/json"), None
    chunks = []
    size = 0
    async with asyncio.timeout(60):
        async for chunk in request.body:
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                return None, Response(b'{"error":"REQUEST_TOO_LARGE"}', status=413, content_type="application/json"), None
            chunks.append(chunk)
    body = b"".join(chunks)
    key_id = request.headers.get("X-DocMind-Key-Id", "")
    docmind_worker_auth.verify(
        method=request.method,
        path_and_query=_path_and_query(),
        body=body,
        key_id=key_id,
        timestamp=request.headers.get("X-DocMind-Timestamp", ""),
        nonce=request.headers.get("X-DocMind-Nonce", ""),
        content_sha256=request.headers.get("X-DocMind-Content-SHA256", "").lower(),
        signature=request.headers.get("X-DocMind-Signature", "").lower(),
    )
    return key_id, None, body


@manager.route("/cloud-sync/host-worker/changes/session", methods=["POST"])  # noqa: F821
async def change_session():
    key_id = None
    try:
        key_id, failure, body = await _authenticate()
        if failure is not None:
            return failure
        payload = _json_body(body)
        required = {"protocol_version", "source_id", "worker_id", "owner_id", "action"}
        if not isinstance(payload, dict) or type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 2:
            raise docmind_change_service.ChangeConflict("REQUEST_INVALID")
        action = payload.get("action")
        if action == "renew":
            required.add("epoch")
        if set(payload) != required or action not in {"acquire", "renew"}:
            raise docmind_change_service.ChangeConflict("REQUEST_INVALID")
        args = {name: payload[name] for name in ("source_id", "worker_id", "owner_id")}
        result = docmind_change_service.renew_session(**args, epoch=payload["epoch"]) if action == "renew" else docmind_change_service.acquire_session(**args)
        return _signed(result, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_change_service.ChangeConflict as error:
        return _signed({"error": error.code}, key_id, 400 if error.code == "REQUEST_INVALID" else 409)
    except Exception:
        logger.exception("DocMind incremental session failed")
        if key_id:
            return _signed({"error": "INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
