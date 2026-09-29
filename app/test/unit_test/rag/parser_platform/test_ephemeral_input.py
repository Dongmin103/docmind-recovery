from __future__ import annotations

import hashlib
import importlib.util
import sys
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from common import settings
from common.storage_attempt_audit import StorageAttemptAudit
import json

_MODULE_PATH = Path(__file__).parents[4] / "rag" / "parser_platform" / "ephemeral_input.py"
_SPEC = importlib.util.spec_from_file_location("docmind_ephemeral_input", _MODULE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
CleanupRecord = _MODULE.CleanupRecord
EphemeralInputError = _MODULE.EphemeralInputError
EphemeralParserInputAdapter = _MODULE.EphemeralParserInputAdapter
create_ephemeral_parser_input_adapter_from_env = _MODULE.create_ephemeral_parser_input_adapter_from_env


@dataclass
class Recorder:
    records: list[CleanupRecord] = field(default_factory=list)

    def record_cleanup(self, record: CleanupRecord) -> None:
        self.records.append(record)


class Claim(AbstractContextManager[bool]):
    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.released = False

    def __enter__(self) -> bool:
        return self.allowed

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.released = True


@dataclass
class Guard:
    claims: dict[str, Claim]

    def claim_cleanup(self, *, job_id: str, version_id: str, fencing_token: int) -> Claim:
        assert version_id
        assert fencing_token > 0
        return self.claims[job_id]


def _accept(adapter: EphemeralParserInputAdapter, *, job_id: str = "job-1"):
    plaintext = b"synthetic plaintext"
    return adapter.accept(
        job_id=job_id,
        document_id=f"document-{job_id}",
        version_id=f"version-{job_id}",
        fencing_token=1,
        filename="source.pdf",
        plaintext=plaintext,
        plaintext_sha256=hashlib.sha256(plaintext).hexdigest(),
    )


def test_accept_and_consume_keep_all_plaintext_job_scoped_then_remove(tmp_path: Path) -> None:
    recorder = Recorder()
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=recorder)
    receipt = _accept(adapter)
    root_entries = list(adapter.root.iterdir())
    assert len(root_entries) == 1

    def consume(workspace):
        assert workspace.input_path.read_bytes() == b"synthetic plaintext"
        derivative = workspace.write_derived("page-1.png", b"synthetic derivative")
        assert derivative.read_bytes() == b"synthetic derivative"
        assert not hasattr(workspace, "object_uri")
        return "indexed"

    assert adapter.consume(receipt, consume) == "indexed"
    assert list(adapter.root.iterdir()) == []
    assert [(record.state, record.outcome) for record in recorder.records] == [
        ("PENDING", None),
        ("IN_PROGRESS", "SUCCESS"),
        ("COMPLETE", "SUCCESS"),
    ]


def test_consume_audits_container_cleanup_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audited = StorageAttemptAudit(object(), tmp_path)
    monkeypatch.setattr(settings, "STORAGE_IMPL", audited, raising=False)
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=Recorder())
    receipt = _accept(adapter, job_id="a" * 32)
    with audited.attempt("a" * 32, 1):
        adapter.consume(receipt, lambda _workspace: None)
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert [stage["name"] for stage in report["stages"]] == ["container_cleanup"]
    assert report["stages"][0]["job_id"] == "a" * 32


@pytest.mark.parametrize(
    ("raised", "outcome"),
    [(RuntimeError("parse failed"), "FAILED"), (TimeoutError("slow parser"), "TIMEOUT")],
)
def test_consume_cleans_on_failure_and_timeout(tmp_path: Path, raised: Exception, outcome: str) -> None:
    recorder = Recorder()
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=recorder)
    receipt = _accept(adapter)

    def fail(workspace):
        workspace.write_derived("partial.bin", b"partial")
        raise raised

    with pytest.raises(type(raised)):
        adapter.consume(receipt, fail)
    assert list(adapter.root.iterdir()) == []
    assert recorder.records[-1].state == "COMPLETE"
    assert recorder.records[-1].outcome == outcome


def test_accept_rejects_hash_mismatch_and_path_traversal(tmp_path: Path) -> None:
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=Recorder())
    with pytest.raises(EphemeralInputError, match="EPHEMERAL_PLAINTEXT_HASH_MISMATCH"):
        adapter.accept(
            job_id="job",
            document_id="document",
            version_id="version",
            fencing_token=1,
            filename="source.pdf",
            plaintext=b"synthetic",
            plaintext_sha256="0" * 64,
        )
    content = b"synthetic"
    with pytest.raises(EphemeralInputError, match="EPHEMERAL_FILENAME_INVALID"):
        adapter.accept(
            job_id="job",
            document_id="document",
            version_id="version",
            fencing_token=1,
            filename="../outside.pdf",
            plaintext=content,
            plaintext_sha256=hashlib.sha256(content).hexdigest(),
        )
    assert list(adapter.root.iterdir()) == []


def test_consume_rejects_tampered_input_and_still_cleans(tmp_path: Path) -> None:
    recorder = Recorder()
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=recorder)
    receipt = _accept(adapter)
    directory = adapter._directory_for_token(receipt.token)
    (directory / "input" / "source.pdf").write_bytes(b"tampered")
    called = False

    def consume(_workspace):
        nonlocal called
        called = True

    with pytest.raises(EphemeralInputError, match="EPHEMERAL_INPUT_INTEGRITY_FAILED"):
        adapter.consume(receipt, consume)
    assert not called
    assert not directory.exists()
    assert recorder.records[-1].state == "COMPLETE"
    assert recorder.records[-1].outcome == "FAILED"


def test_environment_factory_has_no_unsafe_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOCMIND_EPHEMERAL_PARSER_ROOT", raising=False)
    with pytest.raises(EphemeralInputError, match="EPHEMERAL_ROOT_NOT_CONFIGURED"):
        create_ephemeral_parser_input_adapter_from_env(recorder=Recorder())
    configured = tmp_path / "configured-root"
    monkeypatch.setenv("DOCMIND_EPHEMERAL_PARSER_ROOT", str(configured))
    adapter = create_ephemeral_parser_input_adapter_from_env(recorder=Recorder())
    assert adapter.root == configured


def test_cleanup_failure_is_recorded_and_plaintext_is_not_reported_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder()
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=recorder)
    receipt = _accept(adapter)
    directory = adapter._directory_for_token(receipt.token)

    def fail_remove(_directory: Path) -> None:
        raise PermissionError("synthetic locked file")

    monkeypatch.setattr(_MODULE.shutil, "rmtree", fail_remove)
    with pytest.raises(EphemeralInputError, match="EPHEMERAL_CLEANUP_FAILED"):
        adapter.cleanup(receipt, "FAILED")

    assert directory.exists()
    assert recorder.records[-1].state == "CLEANUP_FAILED"
    assert recorder.records[-1].error_code == "REMOVE_FAILED"
    assert not any(record.state == "COMPLETE" for record in recorder.records)


def test_recorder_failure_does_not_prevent_plaintext_removal(tmp_path: Path) -> None:
    @dataclass
    class FailInProgressRecorder(Recorder):
        failed_once: bool = False

        def record_cleanup(self, record: CleanupRecord) -> None:
            if record.state == "IN_PROGRESS" and not self.failed_once:
                self.failed_once = True
                raise RuntimeError("synthetic database outage")
            self.records.append(record)

    recorder = FailInProgressRecorder()
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=recorder)
    receipt = _accept(adapter)
    directory = adapter._directory_for_token(receipt.token)

    with pytest.raises(EphemeralInputError, match="EPHEMERAL_CLEANUP_STATE_FAILED"):
        adapter.cleanup(receipt, "FAILED")

    assert not directory.exists()
    assert recorder.records[-1].state == "CLEANUP_FAILED"
    assert recorder.records[-1].error_code == "STATE_PERSIST_FAILED"
    assert not any(record.state == "COMPLETE" for record in recorder.records)


def test_reaper_removes_only_stale_exclusively_claimed_workspace(tmp_path: Path) -> None:
    base = datetime(2026, 9, 21, tzinfo=UTC)
    clock = [base]
    recorder = Recorder()
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=recorder, now=lambda: clock[0])
    stale = _accept(adapter, job_id="stale")
    active = _accept(adapter, job_id="active")
    recent = _accept(adapter, job_id="recent")
    clock[0] = base + timedelta(hours=2)
    adapter._write_derived(adapter._directory_for_token(recent.token), "touch.bin", b"x")
    stale_claim = Claim(True)
    active_claim = Claim(False)
    guard = Guard({"stale": stale_claim, "active": active_claim})

    report = adapter.reap(stale_after=timedelta(hours=1), guard=guard)

    assert report.removed == 1
    assert report.active_or_newer == 1
    assert report.too_recent == 1
    assert stale_claim.released and active_claim.released
    assert not adapter._directory_for_token(stale.token).exists()
    assert adapter._directory_for_token(active.token).exists()
    assert adapter._directory_for_token(recent.token).exists()


def test_invalid_or_guard_error_reaper_fails_closed(tmp_path: Path) -> None:
    base = datetime(2026, 9, 21, tzinfo=UTC)
    clock = [base]
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=Recorder(), now=lambda: clock[0])
    receipt = _accept(adapter, job_id="guard-error")
    invalid = adapter.root / "unknown"
    invalid.mkdir()
    clock[0] = base + timedelta(hours=2)

    class ErrorGuard:
        def claim_cleanup(self, **_):
            raise RuntimeError("database unavailable")

    report = adapter.reap(stale_after=timedelta(hours=1), guard=ErrorGuard())
    assert report.removed == 0
    assert report.active_or_newer == 1
    assert report.invalid == 1
    assert adapter._directory_for_token(receipt.token).exists()
    assert invalid.exists()


def test_recent_failed_workspace_reaped_without_shortening_general_ttl(tmp_path):
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=Recorder())
    failed = _accept(adapter, job_id="failed")
    active = _accept(adapter, job_id="active")

    class TerminalGuard:
        def claim_cleanup(self, *, job_id, protected_consumer, **kwargs):
            assert protected_consumer
            return Claim(job_id == "failed")

    report = adapter.reap(stale_after=timedelta(hours=1), guard=Guard({}), terminal_guard=TerminalGuard())
    assert report.removed == 1
    assert not adapter._directory_for_token(failed.token).exists()
    assert adapter._directory_for_token(active.token).exists()


def test_reaper_never_removes_locked_consumer_even_with_failed_db_state(tmp_path):
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=Recorder())
    receipt = _accept(adapter)

    class TerminalGuard:
        def claim_cleanup(self, **kwargs):
            pytest.fail("OS-held consumer lock must prevent reaching DB guard")

    def consume(workspace):
        with pytest.raises(EphemeralInputError, match="EPHEMERAL_INPUT_ALREADY_CONSUMING"):
            adapter.cleanup(receipt, "FAILED")
        report = adapter.reap(stale_after=timedelta(hours=1), guard=Guard({}), terminal_guard=TerminalGuard())
        assert report.removed == 0
        assert report.active_or_newer == 1
        assert workspace.input_path.exists()

    adapter.consume(receipt, consume)
    assert list(adapter.root.iterdir()) == []


def test_missing_workspace_confirmation_is_exact_and_fails_closed_on_missing_root(tmp_path):
    adapter = EphemeralParserInputAdapter(tmp_path / "ephemeral", recorder=Recorder())
    receipt = _accept(adapter)
    digest = hashlib.sha256(receipt.token.encode()).hexdigest()
    assert not adapter.workspace_is_absent(digest)
    assert not adapter.workspace_is_absent("../escape")
    adapter.cleanup(receipt, "FAILED")
    assert adapter.workspace_is_absent(digest)
    adapter.root.rmdir()
    assert not adapter.workspace_is_absent(digest)
