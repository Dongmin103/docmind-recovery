from __future__ import annotations

import io
import os
import signal
import subprocess
import tempfile
import zipfile
from pathlib import Path

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def validate_docx_package(content: bytes, *, max_source_bytes: int) -> None:
    if len(content) > max_source_bytes:
        raise ValueError("legacy DOC conversion output is oversized")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            if not {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                raise ValueError("legacy DOC conversion output is not a DOCX package")
            if sum(info.file_size for info in archive.infolist()) > max_source_bytes * 4:
                raise ValueError("legacy DOC conversion output expands beyond its limit")
    except zipfile.BadZipFile as error:
        raise ValueError("legacy DOC conversion output is not a DOCX package") from error


def convert_legacy_doc(
    source_bytes: bytes,
    *,
    max_source_bytes: int,
    timeout_seconds: int,
    temp_root: str = "/tmp",
) -> bytes:
    """Convert one OLE Word document inside a job-scoped tmpfs directory."""

    if not source_bytes.startswith(OLE_MAGIC) or len(source_bytes) > max_source_bytes:
        raise ValueError("invalid or oversized legacy DOC source")
    with tempfile.TemporaryDirectory(prefix="docmind-legacy-doc-", dir=temp_root) as job_dir_value:
        job_dir = Path(job_dir_value)
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        profile_dir = job_dir / "profile"
        home_dir = job_dir / "home"
        for path in (input_dir, output_dir, profile_dir, home_dir):
            path.mkdir(mode=0o700)
        source_path = input_dir / "source.doc"
        source_path.write_bytes(source_bytes)
        source_path.chmod(0o600)
        command = [
            "/usr/bin/soffice",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--nolockcheck",
            "--norestore",
            f"-env:UserInstallation={profile_dir.as_uri()}",
            "--convert-to",
            "docx:Office Open XML Text",
            "--outdir",
            str(output_dir),
            str(source_path),
        ]
        environment = {
            "HOME": str(home_dir),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(job_dir),
        }
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise ValueError("legacy DOC conversion timed out") from error
        converted_path = output_dir / "source.docx"
        if return_code != 0 or not converted_path.is_file() or converted_path.is_symlink():
            raise ValueError("legacy DOC conversion failed")
        converted_bytes = converted_path.read_bytes()
        validate_docx_package(converted_bytes, max_source_bytes=max_source_bytes)
        return converted_bytes
