"""Session-scoped document preparation for the temporary preview API."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from defusedxml import ElementTree as SafeET

ROOT = Path(os.environ.get("DOCMIND_PREVIEW_ROOT", "/run/docmind-previews"))
MAX_SOURCE = 64 * 1024 * 1024
MAX_DERIVED = 128 * 1024 * 1024
MAX_SVG = 16 * 1024 * 1024
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FORMATS = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "hwp", "hwpx"}
DIRECT = {"pdf", "docx", "pptx", "xls", "xlsx"}
PACKAGE_PARTS = {
    "docx": {"[Content_Types].xml", "word/document.xml"},
    "pptx": {"[Content_Types].xml", "ppt/presentation.xml"},
    "xlsx": {"[Content_Types].xml", "xl/workbook.xml"},
    "hwpx": {"mimetype", "Contents/content.hpf"},
}
VIEWERS = {"pdf": "pdf", "docx": "word", "pptx": "powerpoint", "xls": "excel", "xlsx": "excel", "hwp": "hwp", "hwpx": "hwp"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
_BLOCKED_TAGS = {"script", "foreignObject", "iframe", "object", "embed", "audio", "video", "a", "animate", "animateMotion", "animateTransform", "set"}
_DATA_IMAGE = re.compile(r"^data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$", re.IGNORECASE)
_LOCAL_URL = re.compile(r"url\(\s*(['\"]?)#[A-Za-z0-9_.:-]+\1\s*\)", re.IGNORECASE)
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict[str, tuple[threading.Event, subprocess.Popen[bytes] | None]] = {}


class PreviewError(ValueError):
    pass


def begin_operation(session_id: str) -> threading.Event:
    if not _SAFE_NAME.fullmatch(session_id):
        raise PreviewError("INVALID_SESSION_ID")
    with _ACTIVE_LOCK:
        if session_id in _ACTIVE:
            raise PreviewError("PREVIEW_SESSION_BUSY")
        event = threading.Event()
        _ACTIVE[session_id] = (event, None)
        return event


def end_operation(session_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(session_id, None)


def cancel(session_id: str) -> dict[str, bool]:
    if not _SAFE_NAME.fullmatch(session_id):
        raise PreviewError("INVALID_SESSION_ID")
    with _ACTIVE_LOCK:
        active = _ACTIVE.get(session_id)
        if active is not None:
            event, process = active
            event.set()
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
    return {"cancelled": True}


def _assert_no_links(path: Path, boundary: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise PreviewError("UNSAFE_PREVIEW_PATH")
        if current == boundary:
            return
        if current == current.parent:
            raise PreviewError("UNSAFE_PREVIEW_PATH")
        current = current.parent


def session_paths(session_id: str, input_path: str, output_dir: str) -> tuple[Path, Path]:
    if not _SAFE_NAME.fullmatch(session_id):
        raise PreviewError("INVALID_SESSION_ID")
    root = ROOT.resolve(strict=True)
    if not root.is_dir() or ROOT.is_symlink():
        raise PreviewError("UNSAFE_PREVIEW_ROOT")
    session = root / session_id
    source, output = Path(input_path), Path(output_dir)
    if not source.is_absolute() or not output.is_absolute():
        raise PreviewError("UNSAFE_PREVIEW_PATH")
    for path in (session, source, output):
        try:
            path.relative_to(session)
        except ValueError as error:
            raise PreviewError("UNSAFE_PREVIEW_PATH") from error
        _assert_no_links(path, root)
        if path.resolve(strict=True) != path:
            raise PreviewError("UNSAFE_PREVIEW_PATH")
    if not source.is_file() or not output.is_dir() or not session.is_dir():
        raise PreviewError("PREVIEW_INPUT_MISSING")
    if not stat.S_ISREG(source.stat().st_mode):
        raise PreviewError("UNSAFE_PREVIEW_PATH")
    if source.stat().st_size < 1 or source.stat().st_size > MAX_SOURCE:
        raise PreviewError("PREVIEW_INPUT_TOO_LARGE")
    return source, output


def validate_package(path: Path, kind: str, limit: int) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if len(entries) > 10000 or len(names) != len(entries) or not PACKAGE_PARTS[kind].issubset(names):
                raise PreviewError("INVALID_PREVIEW_PACKAGE")
            if sum(entry.file_size for entry in entries) > limit * 4:
                raise PreviewError("PREVIEW_PACKAGE_EXPANSION_LIMIT")
            for entry in entries:
                if entry.flag_bits & 0x1 or entry.filename.startswith("/") or ".." in Path(entry.filename).parts or entry.file_size > limit:
                    raise PreviewError("INVALID_PREVIEW_PACKAGE")
    except (zipfile.BadZipFile, OSError) as error:
        raise PreviewError("INVALID_PREVIEW_PACKAGE") from error


def validate_source(path: Path, source_format: str) -> None:
    if source_format not in FORMATS:
        raise PreviewError("UNSUPPORTED_PREVIEW_FORMAT")
    with path.open("rb") as stream:
        magic = stream.read(8)
    if source_format == "pdf" and not magic.startswith(b"%PDF-"):
        raise PreviewError("INVALID_PREVIEW_SOURCE")
    if source_format in {"doc", "ppt", "xls", "hwp"} and magic != OLE_MAGIC:
        raise PreviewError("INVALID_PREVIEW_SOURCE")
    if source_format in PACKAGE_PARTS:
        validate_package(path, source_format, MAX_SOURCE)


def _run(command: list[str], *, timeout: int, session_id: str, cancel_event: threading.Event, environment: dict[str, str] | None = None) -> None:
    if cancel_event.is_set():
        raise PreviewError("PREVIEW_CANCELLED")
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
    )
    with _ACTIVE_LOCK:
        if session_id in _ACTIVE:
            _ACTIVE[session_id] = (cancel_event, process)
        if cancel_event.is_set() and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
    try:
        if process.wait(timeout=timeout) != 0:
            if cancel_event.is_set():
                raise PreviewError("PREVIEW_CANCELLED")
            raise PreviewError("PREVIEW_PROCESS_FAILED")
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise PreviewError("PREVIEW_PROCESS_TIMEOUT") from error
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        with _ACTIVE_LOCK:
            if session_id in _ACTIVE:
                _ACTIVE[session_id] = (cancel_event, None)


def convert_office(source: Path, output_dir: Path, source_format: str, session_id: str, cancel_event: threading.Event) -> Path:
    target_format = {"doc": "docx", "ppt": "pptx"}[source_format]
    scratch = output_dir / "processor-scratch"
    scratch.mkdir(mode=0o700, exist_ok=False)
    profile, home = scratch / "profile", scratch / "home"
    profile.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    staged = scratch / ("source." + source_format)
    # LibreOffice reads its own input path. Hard links avoid a second plaintext copy.
    os.link(source, staged)
    target = output_dir / ("display." + target_format)
    try:
        command = [
            "/usr/bin/soffice",
            "--headless",
            "--nologo",
            "--nodefault",
            "--nofirststartwizard",
            "--nolockcheck",
            "--norestore",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to",
            "docx:Office Open XML Text" if source_format == "doc" else "pptx:Impress MS PowerPoint 2007 XML",
            "--outdir",
            str(output_dir),
            str(staged),
        ]
        environment = {"HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin", "TMPDIR": str(scratch)}
        _run(command, timeout=180, session_id=session_id, cancel_event=cancel_event, environment=environment)
        if cancel_event.is_set():
            raise PreviewError("PREVIEW_CANCELLED")
        produced = output_dir / ("source." + target_format)
        if not produced.is_file() or produced.is_symlink() or produced.stat().st_size > MAX_DERIVED:
            raise PreviewError("PREVIEW_CONVERSION_INVALID")
        validate_package(produced, target_format, MAX_DERIVED)
        if source_format == "doc":
            # Keep the existing DOC conversion package contract in one place.
            from legacy_doc_conversion import validate_docx_package

            validate_docx_package(produced.read_bytes(), max_source_bytes=MAX_DERIVED)
        if target.exists():
            raise PreviewError("PREVIEW_OUTPUT_EXISTS")
        produced.replace(target)
        return target
    finally:
        shutil.rmtree(scratch)


def _local_name(name: str) -> str:
    return name.split("}", 1)[-1]


def _sanitize_node(node: ET.Element) -> None:
    for child in list(node):
        if _local_name(child.tag) in _BLOCKED_TAGS:
            node.remove(child)
        else:
            _sanitize_node(child)
    for key, value in list(node.attrib.items()):
        attr = _local_name(key)
        lower = value.strip().lower()
        if (
            attr.lower().startswith("on")
            or attr in {"href", "src"}
            and not (value.startswith("#") or _DATA_IMAGE.fullmatch(value))
            or "url(" in lower
            and _LOCAL_URL.sub("", lower).find("url(") >= 0
            or "@import" in lower
            or "expression(" in lower
            or "javascript:" in lower
        ):
            del node.attrib[key]
    if _local_name(node.tag) == "style" and node.text:
        css = node.text.lower()
        if "@import" in css or "javascript:" in css or _LOCAL_URL.sub("", css).find("url(") >= 0:
            node.text = ""


def sanitize_svg(raw: bytes) -> bytes:
    if len(raw) < 32 or len(raw) > MAX_SVG or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise PreviewError("INVALID_PREVIEW_SVG")
    try:
        root = SafeET.fromstring(raw)
    except Exception as error:
        raise PreviewError("INVALID_PREVIEW_SVG") from error
    if root.tag != "{" + _SVG_NS + "}svg":
        raise PreviewError("INVALID_PREVIEW_SVG")
    _sanitize_node(root)
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", _XLINK_NS)
    rendered = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if len(rendered) > MAX_SVG:
        raise PreviewError("PREVIEW_SVG_TOO_LARGE")
    return rendered


def render_hwp_page(source: Path, output_dir: Path, source_format: str, page: int, session_id: str, cancel_event: threading.Event) -> tuple[Path, int]:
    if page < 1 or page > 100000:
        raise PreviewError("INVALID_PREVIEW_PAGE")
    raw = output_dir / (f"page-{page}.raw.svg")
    target = output_dir / (f"page-{page}.svg")
    metadata = output_dir / (f"page-{page}.json")
    if target.exists():
        raise PreviewError("PREVIEW_OUTPUT_EXISTS")
    try:
        _run(
            [sys.executable, str(Path(__file__).with_name("page_worker.py")), str(source), source_format, str(page), str(raw), str(metadata)],
            timeout=60,
            session_id=session_id,
            cancel_event=cancel_event,
        )
        if not raw.is_file() or raw.is_symlink() or raw.stat().st_size > MAX_SVG or not metadata.is_file():
            raise PreviewError("PREVIEW_RENDER_INVALID")
        page_count = int(json.loads(metadata.read_text(encoding="utf-8"))["page_count"])
        if page_count < page or page_count > 100000:
            raise PreviewError("PREVIEW_RENDER_INVALID")
        sanitized = sanitize_svg(raw.read_bytes())
        if cancel_event.is_set():
            raise PreviewError("PREVIEW_CANCELLED")
        target.write_bytes(sanitized)
        target.chmod(0o600)
        return target, page_count
    finally:
        raw.unlink(missing_ok=True)
        metadata.unlink(missing_ok=True)


def process(session_id: str, input_path: str, source_format: str, output_dir: str, cancel_event: threading.Event | None = None) -> dict[str, object]:
    source, output = session_paths(session_id, input_path, output_dir)
    validate_source(source, source_format)
    if source_format in DIRECT:
        return {"display_format": source_format, "viewer_kind": VIEWERS[source_format], "page_count": None, "content_path": str(source), "first_page_path": None}
    if source_format in {"doc", "ppt"}:
        target = convert_office(source, output, source_format, session_id, cancel_event or threading.Event())
        display_format = "docx" if source_format == "doc" else "pptx"
        return {"display_format": display_format, "viewer_kind": VIEWERS[display_format], "page_count": None, "content_path": str(target), "first_page_path": None}
    target, page_count = render_hwp_page(source, output, source_format, 1, session_id, cancel_event or threading.Event())
    (output / "page-count.json").write_text(json.dumps({"page_count": page_count}), encoding="utf-8")
    return {"display_format": "svg", "viewer_kind": "hwp", "page_count": page_count, "content_path": None, "first_page_path": str(target)}


def page(session_id: str, input_path: str, source_format: str, output_dir: str, number: int, cancel_event: threading.Event | None = None) -> dict[str, object]:
    if source_format not in {"hwp", "hwpx"}:
        raise PreviewError("UNSUPPORTED_PREVIEW_FORMAT")
    source, output = session_paths(session_id, input_path, output_dir)
    validate_source(source, source_format)
    target = output / f"page-{number}.svg"
    if target.is_file() and not target.is_symlink():
        metadata = output / "page-count.json"
        if metadata.is_file() and not metadata.is_symlink() and 0 < target.stat().st_size <= MAX_SVG:
            count = int(json.loads(metadata.read_text(encoding="utf-8"))["page_count"])
            if 1 <= number <= count <= 100000:
                return {"page_path": str(target), "page_count": count}
        raise PreviewError("PREVIEW_CACHE_INVALID")
    target, count = render_hwp_page(source, output, source_format, number, session_id, cancel_event or threading.Event())
    return {"page_path": str(target), "page_count": count}
