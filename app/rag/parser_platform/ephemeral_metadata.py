from __future__ import annotations

import copy
import json
import re

_EPHEMERAL_REFERENCE_KEYS = {
    "artifact_ref",
    "file_path",
    "media_ref",
    "object_uri",
    "path",
    "raw_artifact_ref",
    "uri",
}
_EPHEMERAL_REFERENCE_SCHEMES = ("artifact://", "file:", "minio://", "s3://")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _strip_ephemeral_references(value, *, workspace_root: str):
    """Remove parser-workspace references while retaining provenance/locators."""

    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in _EPHEMERAL_REFERENCE_KEYS or key_lower.endswith(("_file_path", "_object_uri")):
                continue
            cleaned_item = _strip_ephemeral_references(item, workspace_root=workspace_root)
            if cleaned_item is not None:
                cleaned[key] = cleaned_item
        return cleaned
    if isinstance(value, list):
        return [
            cleaned
            for item in value
            if (cleaned := _strip_ephemeral_references(item, workspace_root=workspace_root)) is not None
        ]
    if isinstance(value, tuple):
        return tuple(
            cleaned
            for item in value
            if (cleaned := _strip_ephemeral_references(item, workspace_root=workspace_root)) is not None
        )
    if isinstance(value, str):
        candidate = value.strip()
        if (
            workspace_root.casefold() in candidate.casefold()
            or candidate.casefold().startswith(_EPHEMERAL_REFERENCE_SCHEMES)
            or _WINDOWS_ABSOLUTE_PATH.match(candidate)
        ):
            return None
    return value


def sanitize_ephemeral_chunk(chunk: dict, workspace) -> dict:
    """Keep searchable content/provenance while dropping all workspace refs."""

    sanitized = copy.deepcopy(chunk)
    sanitized.pop("image", None)
    for key in _EPHEMERAL_REFERENCE_KEYS:
        sanitized.pop(key, None)
    sanitized["img_id"] = ""
    workspace_root = str(workspace.derived_root.resolve())
    sanitized["metadata"] = _strip_ephemeral_references(
        sanitized.get("metadata", {}),
        workspace_root=workspace_root,
    )
    serialized_metadata = json.dumps(sanitized["metadata"], ensure_ascii=False).casefold()
    forbidden = (
        workspace_root.casefold(),
        "artifact://",
        "file:",
        "minio://",
        "s3://",
    )
    if any(marker and marker in serialized_metadata for marker in forbidden):
        raise RuntimeError("ephemeral parser reference remained in chunk metadata")
    return sanitized
