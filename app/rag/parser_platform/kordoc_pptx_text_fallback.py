"""Read native PPTX slide text if the isolated converter cannot render slides."""

from __future__ import annotations

import hashlib
import posixpath
import re
from io import BytesIO
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_SLIDE_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
_SLIDE_PATH = re.compile(r"ppt/slides/slide[0-9]+\.xml\Z")
_MAX_XML_BYTES = 16 * 1024 * 1024
_MAX_XML_TOTAL = 64 * 1024 * 1024
_MAX_TEXT = 4 * 1024 * 1024


def _read_xml(archive: ZipFile, name: str) -> ElementTree.Element:
    info = archive.getinfo(name)
    if info.file_size > _MAX_XML_BYTES or info.compress_size == 0 and info.file_size:
        raise ValueError("PPTX XML exceeds fallback limit")
    with archive.open(info) as stream:
        data = stream.read(_MAX_XML_BYTES + 1)
    if len(data) > _MAX_XML_BYTES or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("PPTX XML is unsafe")
    return ElementTree.fromstring(data)


def pptx_text_result(source_bytes: bytes, *, parser_version: str, patch_revision: str) -> dict:
    """Return a Kordoc-shaped text-only result, with slide provenance and a warning."""
    try:
        with ZipFile(BytesIO(source_bytes)) as archive:
            entries = archive.infolist()
            if (len(entries) > 20_000 or len({entry.filename for entry in entries}) != len(entries)
                    or sum(entry.file_size for entry in entries) > 2 * 1024 * 1024 * 1024):
                raise ValueError("PPTX archive exceeds fallback limit")
            presentation = _read_xml(archive, "ppt/presentation.xml")
            relationships = _read_xml(archive, "ppt/_rels/presentation.xml.rels")
            targets = {}
            for relation in relationships.findall(f"{_PKG}Relationship"):
                if relation.get("Type") != _SLIDE_TYPE or relation.get("TargetMode") == "External":
                    continue
                target = relation.get("Target", "")
                if target.startswith("/") or "\\" in target:
                    raise ValueError("PPTX slide target is unsafe")
                path = posixpath.normpath(posixpath.join("ppt", target))
                if not _SLIDE_PATH.fullmatch(path):
                    raise ValueError("PPTX slide target is unsafe")
                targets[relation.get("Id")] = path
            slide_ids = presentation.find(f"{_P}sldIdLst")
            if slide_ids is None:
                raise ValueError("PPTX has no slides")
            ordered = [targets.get(slide.get(f"{_R}id")) for slide in slide_ids.findall(f"{_P}sldId")]
            if not ordered or any(path is None for path in ordered) or len(set(ordered)) != len(ordered):
                raise ValueError("PPTX slide order is invalid")
            if sum(archive.getinfo(path).file_size for path in ordered) > _MAX_XML_TOTAL:
                raise ValueError("PPTX slides exceed fallback limit")
            blocks = []
            text_size = 0
            for number, path in enumerate(ordered, start=1):
                slide = _read_xml(archive, path)
                for paragraph in slide.iter(f"{_A}p"):
                    value = "".join(node.text or "" for node in paragraph.iter(f"{_A}t")).strip()
                    if value:
                        text_size += len(value)
                        if text_size > _MAX_TEXT:
                            raise ValueError("PPTX text exceeds fallback limit")
                        blocks.append({"type": "paragraph", "text": value, "pageNumber": number})
            if not blocks:
                raise ValueError("PPTX contains no native slide text")
    except (BadZipFile, KeyError, ElementTree.ParseError) as error:
        raise ValueError("PPTX text fallback could not read slides") from error
    return {
        "schema_version": "docmind-kordoc-v2", "parser_name": "kordoc",
        "parser_version": parser_version, "patch_revision": patch_revision,
        "source_format": "pptx", "source_hash": hashlib.sha256(source_bytes).hexdigest(),
        "blocks": blocks, "metadata": {"slideCount": len(ordered)},
        "warnings": [{"code": "PPTX_XML_TEXT_FALLBACK"}],
    }
