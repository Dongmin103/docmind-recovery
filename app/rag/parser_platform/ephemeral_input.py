"""Job-scoped plaintext input for cloud-source parsing.

This module is intentionally independent of the normal upload/object-storage
path. It never imports or calls MinIO/S3 and it does not create durable source,
thumbnail, preview, or chunk-image references.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

CleanupState = Literal["PENDING", "IN_PROGRESS", "COMPLETE", "CLEANUP_FAILED"]
CleanupOutcome = Literal["SUCCESS", "FAILED", "CANCELED", "TIMEOUT", "REAPED"]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FILENAME_RE = re.compile(r"^[^/\\\x00]{1,255}$")
_TOKEN_RE = re.compile(r"^ep-[0-9a-f]{48}$")
_MANIFEST_NAME = ".docmind-ephemeral.json"
_INPUT_DIR = "input"
_DERIVED_DIR = "derived"
_SCHEMA_VERSION = 1
EPHEMERAL_ROOT_ENV = "DOCMIND_EPHEMERAL_PARSER_ROOT"
_T = TypeVar("_T")


class EphemeralInputError(RuntimeError):
    """A stable error code without plaintext content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EphemeralParserInputReceipt:
    """Structural match for ``docmind_ingestion_service.ParserInputReceipt``."""

    token: str


@dataclass(frozen=True)
class CleanupRecord:
    job_id: str
    version_id: str
    fencing_token: int
    state: CleanupState
    outcome: CleanupOutcome | None
    error_code: str | None
    recorded_at: str


class CleanupRecorder(Protocol):
    """Persists job cleanup state; implementations must not store plaintext."""

    def record_cleanup(self, record: CleanupRecord) -> None: ...


class CleanupClaim(AbstractContextManager[bool], Protocol):
    """Exclusive DB-backed claim preventing a processing-lease race."""

    def __enter__(self) -> bool: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool | None: ...


class LeaseCleanupGuard(Protocol):
    """Claims cleanup only when no live/newer processing lease exists."""

    def claim_cleanup(self, *, job_id: str, version_id: str, fencing_token: int) -> CleanupClaim: ...


@dataclass(frozen=True)
class ReapReport:
    examined: int = 0
    removed: int = 0
    active_or_newer: int = 0
    too_recent: int = 0
    invalid: int = 0
    cleanup_failed: int = 0


@dataclass
class _Manifest:
    schema_version: int
    job_id: str
    document_id: str
    version_id: str
    fencing_token: int
    input_name: str
    plaintext_sha256: str
    created_at: str
    touched_at: str
    consumer_lock_protocol: int = 0


@dataclass(frozen=True)
class EphemeralParserWorkspace:
    """Parser-visible local paths valid only during ``consume``."""

    _adapter: EphemeralParserInputAdapter
    _directory: Path
    input_path: Path
    job_id: str
    document_id: str
    version_id: str

    @property
    def derived_root(self) -> Path:
        """Private parser artifact root removed with this workspace."""

        return self._directory / _DERIVED_DIR

    fencing_token: int

    def write_derived(self, filename: str, content: bytes) -> Path:
        """Write a parser derivative without publishing a durable object URI."""

        return self._adapter._write_derived(self._directory, filename, content)


class EphemeralParserInputAdapter:
    """Filesystem adapter for one-job plaintext with mandatory cleanup state."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        recorder: CleanupRecorder,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if recorder is None:
            raise EphemeralInputError("EPHEMERAL_CLEANUP_RECORDER_REQUIRED")
        candidate = Path(os.path.abspath(Path(root).expanduser()))
        if candidate == Path(candidate.anchor):
            raise EphemeralInputError("EPHEMERAL_ROOT_UNSAFE")
        _reject_link_ancestry(candidate)
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        _reject_link_ancestry(candidate)
        _chmod_private_directory(candidate)
        self.root = candidate
        self._recorder = recorder
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    def accept(
        self,
        *,
        job_id: str,
        document_id: str,
        version_id: str,
        fencing_token: int,
        filename: str,
        plaintext: bytes,
        plaintext_sha256: str,
    ) -> EphemeralParserInputReceipt:
        """Create a job workspace and atomically write the decrypted input."""

        _validate_metadata(job_id, document_id, version_id, fencing_token, filename, plaintext_sha256)
        if not isinstance(plaintext, bytes):
            raise EphemeralInputError("EPHEMERAL_PLAINTEXT_TYPE_INVALID")
        actual_hash = hashlib.sha256(plaintext).hexdigest()
        if not hmac.compare_digest(actual_hash, plaintext_sha256):
            raise EphemeralInputError("EPHEMERAL_PLAINTEXT_HASH_MISMATCH")

        token = f"ep-{secrets.token_hex(24)}"
        directory = self._directory_for_token(token)
        with self._lock:
            try:
                directory.mkdir(mode=0o700)
                _chmod_private_directory(directory)
                input_directory = directory / _INPUT_DIR
                derived_directory = directory / _DERIVED_DIR
                input_directory.mkdir(mode=0o700)
                derived_directory.mkdir(mode=0o700)
                _chmod_private_directory(input_directory)
                _chmod_private_directory(derived_directory)
                now = _utc_string(self._now())
                manifest = _Manifest(
                    schema_version=_SCHEMA_VERSION,
                    job_id=job_id,
                    document_id=document_id,
                    version_id=version_id,
                    fencing_token=fencing_token,
                    input_name=filename,
                    plaintext_sha256=plaintext_sha256,
                    created_at=now,
                    touched_at=now,
                    consumer_lock_protocol=1,
                )
                self._write_manifest(directory, manifest)
                _atomic_private_write(input_directory / filename, plaintext)
                self._record(manifest, "PENDING", None, None)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
        return EphemeralParserInputReceipt(token=token)

    def consume(
        self,
        receipt: EphemeralParserInputReceipt | str | Any,
        consumer: Callable[[EphemeralParserWorkspace], _T],
    ) -> _T:
        """Run parse/index consumption and always remove plaintext afterward.

        The callback should include every parallel consumer and indexing handoff
        that still needs a local input or derivative. Returning from the callback
        confirms those consumers are finished. Cleanup uses no canceled request
        context, so cancellation and timeout cannot suppress deletion.
        """

        token = _receipt_token(receipt)
        directory = self._directory_for_token(token)
        with self._lock:
            manifest = self._read_manifest(directory)
            lock_path = directory / ".consume.lock"
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(lock_fd)
            except FileExistsError as error:
                raise EphemeralInputError("EPHEMERAL_INPUT_ALREADY_CONSUMING") from error

        input_path = directory / _INPUT_DIR / manifest.input_name
        workspace = EphemeralParserWorkspace(
            _adapter=self,
            _directory=directory,
            input_path=input_path,
            job_id=manifest.job_id,
            document_id=manifest.document_id,
            version_id=manifest.version_id,
            fencing_token=manifest.fencing_token,
        )
        with self._consumer_lock(directory) as acquired:
            if not acquired:
                raise EphemeralInputError("EPHEMERAL_INPUT_ALREADY_CONSUMING")
            return self._consume_locked(directory, manifest, workspace, consumer)

    def _consume_locked(self, directory, manifest, workspace, consumer):
        input_path = workspace.input_path
        outcome: CleanupOutcome = "FAILED"
        try:
            if _is_link_or_reparse(input_path) or not input_path.is_file():
                raise EphemeralInputError("EPHEMERAL_INPUT_INVALID")
            if not hmac.compare_digest(_sha256_file(input_path), manifest.plaintext_sha256):
                raise EphemeralInputError("EPHEMERAL_INPUT_INTEGRITY_FAILED")
            result = consumer(workspace)
            outcome = "SUCCESS"
            return result
        except TimeoutError:
            outcome = "TIMEOUT"
            raise
        except (KeyboardInterrupt, SystemExit):
            outcome = "CANCELED"
            raise
        finally:
            self._cleanup(directory, manifest, outcome)

    def cleanup(self, receipt: EphemeralParserInputReceipt | str | Any, outcome: CleanupOutcome) -> None:
        """Explicit cleanup for orchestration that does not use ``consume``."""

        token = _receipt_token(receipt)
        directory = self._directory_for_token(token)
        with self._consumer_lock(directory) as acquired:
            if not acquired:
                raise EphemeralInputError("EPHEMERAL_INPUT_ALREADY_CONSUMING")
            manifest = self._read_manifest(directory)
            self._cleanup(directory, manifest, outcome)

    def workspace_is_absent(self, token_hash: str) -> bool:
        """Confirm one persisted token's workspace vanished, e.g. tmpfs reset.

        Caller must hold its expired terminal job's DB cleanup guard. Missing
        or substituted roots are not evidence of successful cleanup.
        """
        if not _SHA256_RE.fullmatch(token_hash):
            return False
        _reject_link_ancestry(self.root)
        if not self.root.is_dir():
            return False
        directory = self.root / f"job-{token_hash[:32]}"
        return not os.path.lexists(directory)

    def reap(self, *, stale_after: timedelta, guard: LeaseCleanupGuard, terminal_guard=None) -> ReapReport:
        """Remove abandoned workspaces only under an exclusive lease-store claim."""

        if stale_after <= timedelta(0):
            raise EphemeralInputError("EPHEMERAL_REAPER_TTL_INVALID")
        if guard is None:
            raise EphemeralInputError("EPHEMERAL_REAPER_GUARD_REQUIRED")
        counts = {
            "examined": 0,
            "removed": 0,
            "active_or_newer": 0,
            "too_recent": 0,
            "invalid": 0,
            "cleanup_failed": 0,
        }
        now = self._now()
        for directory in self.root.iterdir():
            if _is_link_or_reparse(directory) or not directory.is_dir():
                continue
            counts["examined"] += 1
            try:
                manifest = self._read_manifest(directory)
                touched = datetime.fromisoformat(manifest.touched_at)
            except (EphemeralInputError, OSError, ValueError, json.JSONDecodeError):
                counts["invalid"] += 1
                continue
            recent = now - touched < stale_after
            if recent and terminal_guard is None:
                counts["too_recent"] += 1
                continue
            try:
                with self._consumer_lock(directory) as unlocked:
                    if not unlocked:
                        counts["active_or_newer"] += 1
                        continue
                    selected_guard = terminal_guard if recent else guard
                    arguments = {"job_id": manifest.job_id, "version_id": manifest.version_id, "fencing_token": manifest.fencing_token}
                    if recent:
                        arguments["protected_consumer"] = manifest.consumer_lock_protocol == 1
                    with selected_guard.claim_cleanup(**arguments) as claimed:
                        if not claimed:
                            counts["active_or_newer"] += 1
                            continue
                        try:
                            self._cleanup(directory, manifest, "REAPED")
                        except EphemeralInputError:
                            counts["cleanup_failed"] += 1
                        else:
                            counts["removed"] += 1
            except Exception:  # noqa: BLE001 -- an unavailable lease backend must fail closed
                counts["active_or_newer"] += 1
        return ReapReport(**counts)

    @contextmanager
    def _consumer_lock(self, directory: Path):
        """The Docker parser owns this OS lock for the entire consume lifetime.

        A killed process releases flock, unlike the once-only consume marker.
        Lock acquisition precedes the DB cleanup claim to avoid deadlocking a
        consumer that is recording its cleanup under the same row lock.
        """
        import fcntl

        lock_path = directory / ".workspace.lock"
        if _is_link_or_reparse(lock_path):
            raise EphemeralInputError("EPHEMERAL_INPUT_INVALID")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _write_derived(self, directory: Path, filename: str, content: bytes) -> Path:
        _validate_filename(filename)
        if not isinstance(content, bytes):
            raise EphemeralInputError("EPHEMERAL_DERIVATIVE_TYPE_INVALID")
        with self._lock:
            manifest = self._read_manifest(directory)
            destination = directory / _DERIVED_DIR / filename
            _atomic_private_write(destination, content)
            manifest.touched_at = _utc_string(self._now())
            self._write_manifest(directory, manifest)
            return destination

    def _cleanup(self, directory: Path, manifest: _Manifest, outcome: CleanupOutcome) -> None:
        with self._lock:
            state_error: Exception | None = None
            try:
                self._record(manifest, "IN_PROGRESS", outcome, None)
            except Exception as error:  # noqa: BLE001 -- deletion must still run
                state_error = error
            try:
                shutil.rmtree(directory)
            except OSError as error:
                try:
                    self._record(manifest, "CLEANUP_FAILED", outcome, "REMOVE_FAILED")
                except Exception as record_error:  # noqa: BLE001 -- removal error stays primary
                    state_error = state_error or record_error
                raise EphemeralInputError("EPHEMERAL_CLEANUP_FAILED") from error
            if state_error is not None:
                try:
                    self._record(
                        manifest,
                        "CLEANUP_FAILED",
                        outcome,
                        "STATE_PERSIST_FAILED",
                    )
                except Exception as record_error:  # noqa: BLE001 -- original failure remains primary
                    state_error = state_error or record_error
                raise EphemeralInputError("EPHEMERAL_CLEANUP_STATE_FAILED") from state_error
            try:
                self._record(manifest, "COMPLETE", outcome, None)
            except Exception as error:
                try:
                    self._record(
                        manifest,
                        "CLEANUP_FAILED",
                        outcome,
                        "STATE_PERSIST_FAILED",
                    )
                except Exception as record_error:  # noqa: BLE001 -- COMPLETE failure remains primary
                    error.add_note(f"cleanup failure state could not be recorded: {type(record_error).__name__}")
                raise EphemeralInputError("EPHEMERAL_CLEANUP_STATE_FAILED") from error

    def _directory_for_token(self, token: str) -> Path:
        if not _TOKEN_RE.fullmatch(token):
            raise EphemeralInputError("EPHEMERAL_TOKEN_INVALID")
        digest = hashlib.sha256(token.encode()).hexdigest()
        return self.root / f"job-{digest[:32]}"

    def _write_manifest(self, directory: Path, manifest: _Manifest) -> None:
        payload = json.dumps(asdict(manifest), separators=(",", ":"), sort_keys=True).encode()
        _atomic_private_write(directory / _MANIFEST_NAME, payload)

    def _read_manifest(self, directory: Path) -> _Manifest:
        try:
            payload = json.loads((directory / _MANIFEST_NAME).read_text(encoding="utf-8"))
            manifest = _Manifest(**payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise EphemeralInputError("EPHEMERAL_MANIFEST_INVALID") from error
        if manifest.schema_version != _SCHEMA_VERSION:
            raise EphemeralInputError("EPHEMERAL_MANIFEST_INVALID")
        _validate_metadata(
            manifest.job_id,
            manifest.document_id,
            manifest.version_id,
            manifest.fencing_token,
            manifest.input_name,
            manifest.plaintext_sha256,
        )
        return manifest

    def _record(
        self,
        manifest: _Manifest,
        state: CleanupState,
        outcome: CleanupOutcome | None,
        error_code: str | None,
    ) -> None:
        self._recorder.record_cleanup(
            CleanupRecord(
                job_id=manifest.job_id,
                version_id=manifest.version_id,
                fencing_token=manifest.fencing_token,
                state=state,
                outcome=outcome,
                error_code=error_code,
                recorded_at=_utc_string(self._now()),
            )
        )


def create_ephemeral_parser_input_adapter(root: str | os.PathLike[str], *, recorder: CleanupRecorder) -> EphemeralParserInputAdapter:
    """Factory suitable for ``configure_parser_input_adapter(...)``."""

    return EphemeralParserInputAdapter(root, recorder=recorder)


def create_ephemeral_parser_input_adapter_from_env(*, recorder: CleanupRecorder) -> EphemeralParserInputAdapter:
    """Create the adapter from the dedicated runtime root, failing closed."""

    root = os.environ.get(EPHEMERAL_ROOT_ENV, "").strip()
    if not root:
        raise EphemeralInputError("EPHEMERAL_ROOT_NOT_CONFIGURED")
    return create_ephemeral_parser_input_adapter(root, recorder=recorder)


def _validate_metadata(
    job_id: str,
    document_id: str,
    version_id: str,
    fencing_token: int,
    filename: str,
    plaintext_sha256: str,
) -> None:
    for value in (job_id, document_id, version_id):
        if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
            raise EphemeralInputError("EPHEMERAL_IDENTITY_INVALID")
    if not isinstance(fencing_token, int) or isinstance(fencing_token, bool) or fencing_token <= 0:
        raise EphemeralInputError("EPHEMERAL_FENCING_TOKEN_INVALID")
    _validate_filename(filename)
    if not _SHA256_RE.fullmatch(str(plaintext_sha256)):
        raise EphemeralInputError("EPHEMERAL_PLAINTEXT_HASH_INVALID")


def _validate_filename(filename: str) -> None:
    if not isinstance(filename, str) or not _SAFE_FILENAME_RE.fullmatch(filename) or filename in {".", ".."} or Path(filename).name != filename:
        raise EphemeralInputError("EPHEMERAL_FILENAME_INVALID")


def _receipt_token(receipt: EphemeralParserInputReceipt | str | Any) -> str:
    token = receipt if isinstance(receipt, str) else getattr(receipt, "token", None)
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise EphemeralInputError("EPHEMERAL_TOKEN_INVALID")
    return token


def _atomic_private_write(destination: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".writing-", dir=destination.parent)
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _chmod_private_directory(directory: Path) -> None:
    os.chmod(directory, 0o700)


def _reject_link_ancestry(path: Path) -> None:
    current = path
    while True:
        if _is_link_or_reparse(current):
            raise EphemeralInputError("EPHEMERAL_ROOT_LINK_UNSAFE")
        if current == current.parent:
            return
        current = current.parent


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_attribute)


def _utc_string(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
