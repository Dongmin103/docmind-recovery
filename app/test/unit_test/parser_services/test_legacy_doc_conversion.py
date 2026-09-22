from __future__ import annotations

import importlib.util
import io
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "parser_services" / "docling_office" / "legacy_doc_conversion.py"
SPEC = importlib.util.spec_from_file_location("legacy_doc_conversion_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
conversion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(conversion)


def _docx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
    return output.getvalue()


class FakeProcess:
    pid = 4242

    def __init__(self, command, *, output: bytes | None = None, return_code: int = 0, timeout: bool = False):
        self.return_code = return_code
        self.timeout = timeout
        self.wait_count = 0
        if output is not None:
            output_dir = Path(command[command.index("--outdir") + 1])
            (output_dir / "source.docx").write_bytes(output)

    def wait(self, timeout=None):
        self.wait_count += 1
        if self.timeout and self.wait_count == 1:
            raise subprocess.TimeoutExpired("soffice", timeout)
        return self.return_code


def _source() -> bytes:
    return conversion.OLE_MAGIC + b"legacy-word-fixture"


def _run(monkeypatch, tmp_path: Path, process_factory):
    monkeypatch.setattr(conversion.subprocess, "Popen", process_factory)
    return conversion.convert_legacy_doc(
        _source(),
        max_source_bytes=1024 * 1024,
        timeout_seconds=1,
        temp_root=str(tmp_path),
    )


def test_legacy_doc_conversion_accepts_only_a_real_docx_package_and_cleans_workspace(monkeypatch, tmp_path: Path):
    converted = _docx_bytes()
    result = _run(monkeypatch, tmp_path, lambda command, **_kwargs: FakeProcess(command, output=converted))
    assert result == converted
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("output", [b"PKnot-a-zip", None])
def test_legacy_doc_conversion_failure_cleans_workspace(monkeypatch, tmp_path: Path, output: bytes | None):
    return_code = 0 if output is not None else 1
    with pytest.raises(ValueError, match="conversion"):
        _run(
            monkeypatch,
            tmp_path,
            lambda command, **_kwargs: FakeProcess(command, output=output, return_code=return_code),
        )
    assert list(tmp_path.iterdir()) == []


def test_legacy_doc_conversion_timeout_kills_process_group_and_cleans_workspace(monkeypatch, tmp_path: Path):
    killed = []
    monkeypatch.setattr(conversion.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(ValueError, match="timed out"):
        _run(monkeypatch, tmp_path, lambda command, **_kwargs: FakeProcess(command, timeout=True))
    assert killed and killed[0][0] == FakeProcess.pid
    assert list(tmp_path.iterdir()) == []


def test_legacy_doc_conversion_rejects_non_ole_before_starting_process(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        conversion.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("converter process must not start"),
    )
    with pytest.raises(ValueError, match="legacy DOC source"):
        conversion.convert_legacy_doc(
            b"PK-extension-masquerade",
            max_source_bytes=1024,
            timeout_seconds=1,
            temp_root=str(tmp_path),
        )
    assert list(tmp_path.iterdir()) == []
