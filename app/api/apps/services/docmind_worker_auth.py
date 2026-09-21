from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from base64 import b64decode
from binascii import Error as BinasciiError
from datetime import UTC, datetime, timedelta
from pathlib import Path

from peewee import IntegrityError

from api.db.db_models import DocmindWorkerRequestNonce
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp

HEX_RE = re.compile(r"^[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
MAX_CLOCK_SKEW_SECONDS = 300


class WorkerAuthenticationError(RuntimeError):
    pass


def _secret(key_id: str) -> bytes:
    configured_key_id = os.getenv("DOCMIND_HOST_WORKER_KEY_ID", "windows-host-1")
    if not hmac.compare_digest(key_id, configured_key_id):
        raise WorkerAuthenticationError("WORKER_KEY_UNKNOWN")
    secret_file = os.getenv("DOCMIND_HOST_WORKER_HMAC_SECRET_FILE", "").strip()
    if not secret_file:
        raise WorkerAuthenticationError("WORKER_AUTH_NOT_CONFIGURED")
    try:
        encoded = Path(secret_file).read_bytes()
    except OSError as error:
        raise WorkerAuthenticationError("WORKER_AUTH_NOT_CONFIGURED") from error
    if len(encoded) == 32:
        return encoded
    try:
        text = encoded.decode("ascii").strip()
        if re.fullmatch(r"[0-9a-fA-F]{64}", text):
            secret = bytes.fromhex(text)
        else:
            secret = b64decode(text, validate=True)
    except (UnicodeDecodeError, ValueError, BinasciiError) as error:
        raise WorkerAuthenticationError("WORKER_AUTH_SECRET_INVALID") from error
    if len(secret) != 32:
        raise WorkerAuthenticationError("WORKER_AUTH_SECRET_INVALID")
    return secret


def canonical(method: str, path_and_query: str, timestamp: str, nonce: str, content_sha256: str) -> bytes:
    return f"{method.upper()}\n{path_and_query}\n{timestamp}\n{nonce}\n{content_sha256}".encode()


def verify(
    *,
    method: str,
    path_and_query: str,
    body: bytes,
    key_id: str,
    timestamp: str,
    nonce: str,
    content_sha256: str,
    signature: str,
) -> None:
    if not NONCE_RE.fullmatch(nonce) or not HEX_RE.fullmatch(content_sha256) or not HEX_RE.fullmatch(signature):
        raise WorkerAuthenticationError("WORKER_AUTH_HEADER_INVALID")
    try:
        timestamp_value = int(timestamp)
    except ValueError as error:
        raise WorkerAuthenticationError("WORKER_AUTH_TIMESTAMP_INVALID") from error
    if abs(int(time.time()) - timestamp_value) > MAX_CLOCK_SKEW_SECONDS:
        raise WorkerAuthenticationError("WORKER_AUTH_TIMESTAMP_EXPIRED")
    actual_content_hash = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual_content_hash, content_sha256):
        raise WorkerAuthenticationError("WORKER_AUTH_CONTENT_HASH_MISMATCH")
    expected = hmac.new(
        _secret(key_id),
        canonical(method, path_and_query, timestamp, nonce, content_sha256),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WorkerAuthenticationError("WORKER_AUTH_SIGNATURE_INVALID")

    now = datetime.now(UTC).replace(tzinfo=None)
    nonce_hash = hashlib.sha256(f"{key_id}\x1f{nonce}".encode()).hexdigest()
    database = DocmindWorkerRequestNonce._meta.database
    with database.atomic():
        DocmindWorkerRequestNonce.delete().where(DocmindWorkerRequestNonce.expires_at <= now).execute()
        try:
            DocmindWorkerRequestNonce.create(
                id=get_uuid(),
                key_id=key_id,
                nonce_hash=nonce_hash,
                expires_at=now + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS * 2),
                create_time=current_timestamp(),
                create_date=datetime.now(UTC).replace(tzinfo=None),
                update_time=current_timestamp(),
                update_date=datetime.now(UTC).replace(tzinfo=None),
            )
        except IntegrityError as error:
            raise WorkerAuthenticationError("WORKER_AUTH_REPLAYED") from error


def signed_json_body(
    payload: dict,
    *,
    method: str,
    path_and_query: str,
    key_id: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    selected_key_id = key_id or os.getenv("DOCMIND_HOST_WORKER_KEY_ID", "windows-host-1")
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    content_sha256 = hashlib.sha256(body).hexdigest()
    signature = hmac.new(
        _secret(selected_key_id),
        canonical(method, path_and_query, timestamp, nonce, content_sha256),
        hashlib.sha256,
    ).hexdigest()
    return body, {
        "X-DocMind-Key-Id": selected_key_id,
        "X-DocMind-Timestamp": timestamp,
        "X-DocMind-Nonce": nonce,
        "X-DocMind-Content-SHA256": content_sha256,
        "X-DocMind-Signature": signature,
    }
