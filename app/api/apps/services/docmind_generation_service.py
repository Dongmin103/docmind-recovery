from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests

from api.apps.services import (
    docmind_api_service,
    docmind_catalog_shadow_service,
    docmind_draft_service,
)
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindDocumentRoutingDigest,
    DocmindDraftChange,
    DocmindFolder,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindProject,
    Document,
)
from api.db.joint_services.tenant_model_service import (
    get_model_config_from_provider_instance,
    get_tenant_default_model_by_type,
)
from api.db.services.llm_service import LLMBundle
from common import settings
from common.constants import LLMType, StatusEnum, TaskStatus
from common.doc_store.doc_store_base import OrderByExpr
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp
from rag.nlp import search as rag_search


CAPABILITY_ARTIFACT = Path(
    os.environ.get(
        "DOCMIND_OPENVIKING_CAPABILITY_ARTIFACT",
        "/ragflow/private/docmind-openviking-capability.json",
    )
)
GENERATOR_SCRIPT = Path("/ragflow/scripts/docmind_generate_draft.py")
GENERATOR_LOG_ROOT = Path("/ragflow/logs")
PROMPT_VERSION = "docmind-contrastive-five-card-ko-en-v2"
DIGEST_MODEL_VERSION = "docmind-extractive-routing-digest-v1"
DIGEST_CONFIG_VERSION = "digest-4-excerpts-24-keywords-unicode-safe-v2"
GENERATION_CONFIG_VERSION = "five-card-openviking-semantic-and-vectors-ko-en-v2"
HIERARCHY_PROMPT_VERSION = "docmind-hierarchy-contrastive-ko-v3"
HIERARCHY_CONFIG_VERSION = "hierarchy-detailed-openviking-ovpack-v3"
ROUTING_CARD_LANGUAGE_POLICY = "ko-primary-with-en-terms-v1"
GENERATOR_MODEL_ENV = "DOCMIND_GENERATOR_MODEL"
CARD_GENERATION = {
    "temperature": 0.1,
    "enable_thinking": False,
    "response_format": {"type": "json_object"},
}
CARD_MAX_TOKENS = 8192
HIERARCHY_CARD_CONCURRENCY = 2
HIERARCHY_L0_LENGTH = (80, 180)
HIERARCHY_L1_LENGTH = (350, 700)
HIERARCHY_DOCUMENT_SAMPLE_LIMIT = 64
HIERARCHY_DOCUMENT_KEYWORD_LIMIT = 12
HIERARCHY_AGGREGATE_KEYWORD_LIMIT = 64
HIERARCHY_DOCUMENT_EXCERPT_LIMIT = 1
VALIDATION_TTL_DAYS = 7
ROUTER_POSITIVE_MINIMUM = 68
ROUTER_MULTI_ALL_MINIMUM = 5
PROBE_QUERY_COUNT = 3
VISIBLE_GENERATION_STATES = frozenset(
    {"DRAFT", "GENERATING", "GENERATED", "VALIDATING", "READY", "FAILED"}
)
_WORD_PATTERN = re.compile(r"[0-9A-Za-z가-힣][0-9A-Za-z가-힣_\-]{1,}")
_HANGUL_PATTERN = re.compile(r"[가-힣]")
_STOPWORDS = frozenset(
    {
        "and",
        "are",
        "for",
        "from",
        "that",
        "the",
        "this",
        "with",
        "및",
        "대한",
        "위한",
        "있는",
        "한다",
        "한다면",
        "하는",
        "에서",
        "으로",
        "그리고",
    }
)
_FAILED_SIDECAR_MARKERS = frozenset(
    {
        "[directory overview is not generated]",
    }
)


class DocmindGenerationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _generator_model_config(tenant_id: str) -> dict[str, Any]:
    model_name = os.environ.get(GENERATOR_MODEL_ENV, "").strip()
    try:
        if model_name:
            return get_model_config_from_provider_instance(
                tenant_id,
                LLMType.CHAT,
                model_name,
            )
        return get_tenant_default_model_by_type(tenant_id, LLMType.CHAT)
    except (LookupError, ValueError) as error:
        raise DocmindGenerationError("DOCMIND_GENERATOR_MODEL_UNAVAILABLE") from error


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def routing_digest_json(value: Any) -> str:
    """Serialize digest evidence without relying on the database Unicode width."""
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_json(value: Any) -> str:
    return _hash_bytes(_json(value).encode())


def _timestamps() -> dict[str, Any]:
    now = datetime.now()
    stamp = current_timestamp()
    return {
        "create_time": stamp,
        "create_date": now,
        "update_time": stamp,
        "update_date": now,
    }


def _updates() -> dict[str, Any]:
    return {"update_time": current_timestamp(), "update_date": datetime.now()}


def _normalize_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized[:maximum]


def _validate_generated_sidecar(
    l0: str,
    l1: str,
    *,
    require_korean: bool = True,
) -> None:
    normalized = f"{l0}\n{l1}".lower()
    if any(marker in normalized for marker in _FAILED_SIDECAR_MARKERS):
        raise DocmindGenerationError(
            "DOCMIND_OPENVIKING_SIDECAR_GENERATION_FAILED"
        )
    if require_korean and (
        not _HANGUL_PATTERN.search(l0) or not _HANGUL_PATTERN.search(l1)
    ):
        raise DocmindGenerationError("DOCMIND_OPENVIKING_SIDECAR_LANGUAGE_INVALID")


def validate_capability_artifact(path: Path = CAPABILITY_ARTIFACT) -> dict[str, Any]:
    if not path.is_file():
        raise DocmindGenerationError("DOCMIND_OPENVIKING_CAPABILITY_MISSING")
    try:
        artifact = json.loads(path.read_text())
    except Exception as error:
        raise DocmindGenerationError("DOCMIND_OPENVIKING_CAPABILITY_INVALID") from error
    required = {
        "schema",
        "status",
        "processing_contract",
        "runtime_before",
        "runtime_after_restart",
        "first_before_restart",
        "first_after_restart",
        "second_root",
        "missing_root_detected",
        "active_catalog_or_serving_root_changed",
    }
    if not isinstance(artifact, dict) or not required <= set(artifact):
        raise DocmindGenerationError("DOCMIND_OPENVIKING_CAPABILITY_INVALID")
    contract = artifact["processing_contract"]
    if (
        artifact["status"] != "PASS"
        or artifact["runtime_before"].get("image_id")
        != artifact["runtime_after_restart"].get("image_id")
        or artifact["first_before_restart"] != artifact["first_after_restart"]
        or artifact["missing_root_detected"] is not True
        or artifact["active_catalog_or_serving_root_changed"] is not False
        or contract
        != {
            "processing_mode": "semantic_and_vectors",
            "source_name": "manifest.md",
            "strict": True,
            "telemetry": False,
            "wait": True,
            "watch_interval": 0,
        }
    ):
        raise DocmindGenerationError("DOCMIND_OPENVIKING_CAPABILITY_FAILED")
    return {
        "sha256": _hash_bytes(path.read_bytes()),
        "image_id": artifact["runtime_after_restart"]["image_id"],
        "contract": contract,
    }


def _sample_excerpts(chunks: list[dict[str, Any]], count: int = 4) -> list[str]:
    contents = [
        _normalize_text(row.get("content_with_weight"), 500)
        for row in chunks
        if _normalize_text(row.get("content_with_weight"), 500)
    ]
    if len(contents) <= count:
        return [value[:260] for value in contents]
    indices = sorted(
        {
            round(index * (len(contents) - 1) / (count - 1))
            for index in range(count)
        }
    )
    return [contents[index][:260] for index in indices]


def build_routing_digest(
    *,
    document_name: str,
    chunks: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    if not chunks:
        raise DocmindGenerationError("DOCMIND_ROUTING_DIGEST_CHUNKS_EMPTY")
    normalized_rows = []
    tokens: Counter[str] = Counter()
    for row in chunks:
        chunk_id = str(row.get("id") or row.get("chunk_id") or "")
        content = _normalize_text(row.get("content_with_weight"), 20000)
        if not chunk_id or not content:
            continue
        normalized_rows.append(
            {
                "id": chunk_id,
                "content_sha256": _hash_bytes(content.encode()),
            }
        )
        tokens.update(
            token.lower()
            for token in _WORD_PATTERN.findall(content)
            if token.lower() not in _STOPWORDS
        )
    if not normalized_rows:
        raise DocmindGenerationError("DOCMIND_ROUTING_DIGEST_CHUNKS_EMPTY")
    normalized_rows.sort(key=lambda row: row["id"])
    chunk_set_fingerprint = _hash_json(normalized_rows)
    ordered_chunks = sorted(
        chunks,
        key=lambda row: str(row.get("id") or row.get("chunk_id") or ""),
    )
    output = {
        "schema": "docmind-routing-digest-v1",
        "document_name": _normalize_text(document_name, 255),
        "chunk_count": len(normalized_rows),
        "keywords": [
            token
            for token, _count in sorted(
                tokens.items(),
                key=lambda item: (-item[1], item[0]),
            )[:24]
        ],
        "representative_excerpts": _sample_excerpts(ordered_chunks),
    }
    return output, chunk_set_fingerprint


def _folder_manifest(folder_name: str, l0: str, l1: str) -> str:
    return (
        f"# {folder_name}\n\n"
        "## Routing responsibility\n\n"
        f"{l0.strip()}\n\n"
        "## Detailed routing guidance\n\n"
        f"{l1.strip()}\n"
    )


def normalize_generated_cards(raw: Any, folder_ids: Iterable[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"folders"}:
        raise DocmindGenerationError("DOCMIND_CARD_OUTPUT_SCHEMA_INVALID")
    folders = raw.get("folders")
    expected = tuple(folder_ids)
    if not isinstance(folders, list) or len(folders) != len(expected):
        raise DocmindGenerationError("DOCMIND_CARD_OUTPUT_SCHEMA_INVALID")
    normalized: dict[str, Any] = {}
    for item in folders:
        if not isinstance(item, dict) or set(item) != {"id", "l0", "l1"}:
            raise DocmindGenerationError("DOCMIND_CARD_OUTPUT_SCHEMA_INVALID")
        folder_id = item.get("id")
        l0 = _normalize_text(item.get("l0"), 800)
        l1 = _normalize_text(item.get("l1"), 3500)
        if (
            folder_id not in expected
            or folder_id in normalized
            or len(l0) < 40
            or len(l1) < 120
            or not _HANGUL_PATTERN.search(l0)
            or not _HANGUL_PATTERN.search(l1)
            or "viking://" in l0.lower()
            or "viking://" in l1.lower()
        ):
            raise DocmindGenerationError("DOCMIND_CARD_OUTPUT_INVALID")
        normalized[folder_id] = {"l0": l0, "l1": l1}
    if tuple(normalized) != expected:
        normalized = {folder_id: normalized[folder_id] for folder_id in expected}
    return normalized


def _extract_json_object(response: str) -> dict[str, Any]:
    text = re.sub(r"^.*?</think>", "", response, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise DocmindGenerationError("DOCMIND_CARD_OUTPUT_NOT_JSON")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise DocmindGenerationError("DOCMIND_CARD_OUTPUT_NOT_JSON") from error
    if not isinstance(value, dict):
        raise DocmindGenerationError("DOCMIND_CARD_OUTPUT_NOT_JSON")
    return value


class OpenVikingStagingClient:
    def __init__(self):
        self.base_url = os.environ.get(
            "DOCMIND_OPENVIKING_URL",
            "http://host.docker.internal:1933",
        ).rstrip("/")
        key = os.environ.get("DOCMIND_OPENVIKING_API_KEY", "").strip()
        key_path = os.environ.get("DOCMIND_OPENVIKING_API_KEY_FILE", "").strip()
        if not key and key_path:
            try:
                key = Path(key_path).read_text().strip()
            except OSError as error:
                raise DocmindGenerationError("DOCMIND_OPENVIKING_KEY_UNAVAILABLE") from error
        if not key:
            raise DocmindGenerationError("DOCMIND_OPENVIKING_KEY_UNAVAILABLE")
        self.headers = {"X-API-Key": key}

    def _result(self, response: requests.Response, code: str) -> Any:
        try:
            payload = response.json()
        except Exception as error:
            raise DocmindGenerationError(code) from error
        if response.status_code >= 400 or payload.get("status") != "ok":
            raise DocmindGenerationError(code)
        return payload.get("result")

    def get(self, path: str, **query: Any) -> Any:
        try:
            response = requests.get(
                f"{self.base_url}{path}",
                headers=self.headers,
                params=query,
                timeout=45,
            )
        except requests.RequestException as error:
            raise DocmindGenerationError("DOCMIND_OPENVIKING_READ_FAILED") from error
        return self._result(response, "DOCMIND_OPENVIKING_READ_FAILED")

    def content(self, endpoint: str, uri: str) -> str:
        result = self.get(endpoint, uri=uri)
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("content", "text", "data"):
                if isinstance(result.get(key), str):
                    return result[key]
        raise DocmindGenerationError("DOCMIND_OPENVIKING_CONTENT_INVALID")

    def tree(self, root_uri: str) -> list[dict[str, Any]]:
        result = self.get(
            "/api/v1/fs/tree",
            uri=root_uri,
            output="original",
            show_all_hidden="true",
            node_limit=1000,
            level_limit=10,
        )
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise DocmindGenerationError("DOCMIND_OPENVIKING_TREE_INVALID")
        return result

    def root_exists(self, root_uri: str) -> bool:
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/fs/tree",
                headers=self.headers,
                params={
                    "uri": root_uri,
                    "output": "original",
                    "show_all_hidden": "true",
                    "node_limit": 2,
                    "level_limit": 1,
                },
                timeout=30,
            )
        except requests.RequestException as error:
            raise DocmindGenerationError("DOCMIND_OPENVIKING_READ_FAILED") from error
        if response.status_code == 404:
            return False
        result = self._result(response, "DOCMIND_OPENVIKING_READ_FAILED")
        if not isinstance(result, list):
            raise DocmindGenerationError("DOCMIND_OPENVIKING_TREE_INVALID")
        return bool(result)

    def delete_root(self, root_uri: str) -> bool:
        """Delete one isolated DocMind version root and its vector records."""
        try:
            response = requests.delete(
                f"{self.base_url}/api/v1/fs",
                headers=self.headers,
                params={
                    "uri": root_uri,
                    "recursive": "true",
                    "wait": "false",
                },
                timeout=30,
            )
        except requests.RequestException as error:
            raise DocmindGenerationError(
                "DOCMIND_OPENVIKING_ROOT_DELETE_FAILED"
            ) from error
        if response.status_code == 404:
            return False
        result = self._result(
            response,
            "DOCMIND_OPENVIKING_ROOT_DELETE_FAILED",
        )
        if not isinstance(result, dict) or result.get("uri") != root_uri:
            raise DocmindGenerationError("DOCMIND_OPENVIKING_ROOT_DELETE_FAILED")
        return True

    def no_active_watch(self, target_uri: str) -> bool:
        try:
            response = requests.get(
                f"{self.base_url}/api/v1/watches",
                headers=self.headers,
                params={"to_uri": target_uri, "active_only": "true"},
                timeout=30,
            )
        except requests.RequestException as error:
            raise DocmindGenerationError("DOCMIND_OPENVIKING_WATCH_CHECK_FAILED") from error
        return response.status_code == 404

    def add_manifest(self, target_uri: str, manifest: str) -> None:
        try:
            upload = requests.post(
                f"{self.base_url}/api/v1/resources/temp_upload",
                headers=self.headers,
                files={
                    "file": (
                        "manifest.md",
                        manifest.encode(),
                        "text/markdown",
                    )
                },
                data={"telemetry": "false", "upload_mode": "local"},
                timeout=60,
            )
        except requests.RequestException as error:
            raise DocmindGenerationError("DOCMIND_OPENVIKING_UPLOAD_FAILED") from error
        result = self._result(upload, "DOCMIND_OPENVIKING_UPLOAD_FAILED")
        temp_file_id = result.get("temp_file_id") if isinstance(result, dict) else None
        if not isinstance(temp_file_id, str) or not temp_file_id:
            raise DocmindGenerationError("DOCMIND_OPENVIKING_UPLOAD_FAILED")
        try:
            added = requests.post(
                f"{self.base_url}/api/v1/resources",
                headers={**self.headers, "Content-Type": "application/json"},
                json={
                    "temp_file_id": temp_file_id,
                    "to": target_uri,
                    "reason": "DocMind immutable Catalog draft",
                    "instruction": (
                        "L0 abstract와 L1 overview의 설명 문장은 한국어로 작성하세요. "
                        "GMP, ICH, CAPA, Change Control 등 국제 표준 용어와 약어는 "
                        "영문을 괄호로 병기하고, 폴더 ID와 문서명은 원문을 유지하세요."
                    ),
                    "wait": True,
                    "timeout": 600,
                    "strict": True,
                    "source_name": "manifest.md",
                    "telemetry": False,
                    "watch_interval": 0,
                    "processing_mode": "semantic_and_vectors",
                },
                timeout=660,
            )
        except requests.RequestException as error:
            raise DocmindGenerationError("DOCMIND_OPENVIKING_STAGE_FAILED") from error
        self._result(added, "DOCMIND_OPENVIKING_STAGE_FAILED")

    @staticmethod
    def _manual_ovpack(root_name: str, cards: list[dict[str, str]]) -> bytes:
        contents: dict[str, bytes] = {}
        directories = [""]
        index_records = []
        for index, card in enumerate(cards):
            folder_id = card["folder_id"]
            l0 = card["l0"]
            l1 = card["l1"]
            manifest = _folder_manifest(folder_id, l0, l1)
            directories.extend((folder_id, f"{folder_id}/manifest.md"))
            contents.update(
                {
                    f"{folder_id}/.abstract.md": l0.encode(),
                    f"{folder_id}/.overview.md": l1.encode(),
                    f"{folder_id}/manifest.md/.abstract.md": l0.encode(),
                    f"{folder_id}/manifest.md/.overview.md": l1.encode(),
                    f"{folder_id}/manifest.md/manifest.md": manifest.encode(),
                }
            )
            record_number = index * 4
            for offset, (path, level, text) in enumerate(
                (
                    (folder_id, 0, l0),
                    (folder_id, 1, l1),
                    (f"{folder_id}/manifest.md", 0, l0),
                    (f"{folder_id}/manifest.md", 1, l1),
                ),
                start=1,
            ):
                index_records.append(
                    {
                        "record_id": f"r{record_number + offset:06d}",
                        "path": path,
                        "kind": "directory",
                        "level": level,
                        "text": text,
                        "scalars": {
                            "context_type": "resource",
                            "level": level,
                            "abstract": l0,
                        },
                    }
                )
        file_entries = [
            {
                "path": path,
                "kind": "file",
                "size": len(data),
                "sha256": _hash_bytes(data),
            }
            for path, data in sorted(contents.items())
        ]
        entries = [
            {"path": path, "kind": "directory", "size": 0}
            for path in directories
        ] + file_entries
        index_bytes = (
            "\n".join(_json(record) for record in index_records) + "\n"
        ).encode()
        content_projection = [
            {
                "path": entry["path"],
                "size": entry["size"],
                "sha256": entry["sha256"],
            }
            for entry in file_entries
        ]
        manifest = {
            "kind": "openviking.ovpack",
            "format_version": 3,
            "root": {
                "name": root_name,
                "uri": f"viking://resources/{root_name}",
                "scope": "resources",
            },
            "entries": entries,
            "content_sha256": _hash_json(content_projection),
            "index": {
                "records": {
                    "path": "_ovpack/index_records.jsonl",
                    "count": len(index_records),
                    "sha256": _hash_bytes(index_bytes),
                }
            },
        }
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            archive.writestr(f"{root_name}/", b"")
            archive.writestr(f"{root_name}/files/", b"")
            for directory in directories[1:]:
                archive.writestr(f"{root_name}/files/{directory}/", b"")
            for path, data in sorted(contents.items()):
                archive.writestr(f"{root_name}/files/{path}", data)
            archive.writestr(f"{root_name}/_ovpack/", b"")
            archive.writestr(f"{root_name}/_ovpack/index_records.jsonl", index_bytes)
            archive.writestr(
                f"{root_name}/_ovpack/manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(),
            )
        return buffer.getvalue()

    def import_exact_cards(
        self,
        root_name: str,
        cards: list[dict[str, str]],
    ) -> tuple[str, dict[str, Any]]:
        pack = self._manual_ovpack(root_name, cards)
        try:
            upload = requests.post(
                f"{self.base_url}/api/v1/resources/temp_upload",
                headers=self.headers,
                files={"file": ("manual-card.ovpack", pack, "application/zip")},
                data={"telemetry": "false", "upload_mode": "local"},
                timeout=60,
            )
        except requests.RequestException as error:
            raise DocmindGenerationError("DOCMIND_MANUAL_CARD_ROOT_UNSUPPORTED") from error
        uploaded = self._result(upload, "DOCMIND_MANUAL_CARD_ROOT_UNSUPPORTED")
        temp_file_id = uploaded.get("temp_file_id") if isinstance(uploaded, dict) else None
        if not isinstance(temp_file_id, str) or not temp_file_id:
            raise DocmindGenerationError("DOCMIND_MANUAL_CARD_ROOT_UNSUPPORTED")
        try:
            response = requests.post(
                f"{self.base_url}/api/v1/pack/import",
                headers={**self.headers, "Content-Type": "application/json"},
                json={
                    "temp_file_id": temp_file_id,
                    "parent": "viking://resources",
                    "on_conflict": "fail",
                    "vector_mode": "recompute",
                },
                timeout=180,
            )
        except requests.RequestException as error:
            raise DocmindGenerationError("DOCMIND_MANUAL_CARD_ROOT_UNSUPPORTED") from error
        if response.status_code == 409:
            raise DocmindGenerationError("DOCMIND_MANUAL_CARD_CONFLICT")
        imported = self._result(response, "DOCMIND_MANUAL_CARD_ROOT_UNSUPPORTED")
        uri = imported.get("uri") if isinstance(imported, dict) else None
        expected_uri = f"viking://resources/{root_name}"
        if not isinstance(uri, str) or uri.rstrip("/") != expected_uri:
            raise DocmindGenerationError("DOCMIND_MANUAL_CARD_ROOT_DRIFT")
        root_uri = f"{expected_uri}/"
        for card in cards:
            folder_id = card["folder_id"]
            for suffix, endpoint, expected in (
                (".abstract.md", "/api/v1/content/abstract", card["l0"]),
                (".overview.md", "/api/v1/content/overview", card["l1"]),
                ("manifest.md/.abstract.md", "/api/v1/content/abstract", card["l0"]),
                ("manifest.md/.overview.md", "/api/v1/content/overview", card["l1"]),
            ):
                actual = self.content(endpoint, f"{root_uri}{folder_id}/{suffix}")
                if actual.encode() != expected.encode():
                    raise DocmindGenerationError("DOCMIND_MANUAL_CARD_ROOT_DRIFT")
        return root_uri, self.manual_root_identity(
            root_uri,
            [card["folder_id"] for card in cards],
        )

    def manual_root_identity(
        self,
        root_uri: str,
        folder_ids: Iterable[str],
    ) -> dict[str, Any]:
        rows = self.tree(root_uri)
        suffixes = []
        by_suffix = {}
        for row in rows:
            uri = row.get("uri")
            if not isinstance(uri, str) or not uri.startswith(root_uri):
                raise DocmindGenerationError("DOCMIND_OPENVIKING_ROOT_SCOPE_INVALID")
            suffix = uri[len(root_uri) :].rstrip("/")
            if suffix:
                suffixes.append(suffix)
                by_suffix[suffix] = uri
        sidecars = []
        for folder_id in folder_ids:
            record = {"folder_id": folder_id}
            for key, suffix, endpoint in (
                ("direct_l0_sha256", ".abstract.md", "/api/v1/content/abstract"),
                ("direct_l1_sha256", ".overview.md", "/api/v1/content/overview"),
                (
                    "nested_l0_sha256",
                    "manifest.md/.abstract.md",
                    "/api/v1/content/abstract",
                ),
                (
                    "nested_l1_sha256",
                    "manifest.md/.overview.md",
                    "/api/v1/content/overview",
                ),
            ):
                uri = by_suffix.get(f"{folder_id}/{suffix}")
                if not uri:
                    raise DocmindGenerationError("DOCMIND_OPENVIKING_SIDECAR_MISSING")
                record[key] = _hash_bytes(self.content(endpoint, uri).encode())
            sidecars.append(record)
        identity = {
            "inventory_sha256": _hash_json(sorted(suffixes)),
            "entry_count": len(suffixes),
            "manual_sidecar_set_sha256": _hash_json(sidecars),
        }
        identity["identity_sha256"] = _hash_json(identity)
        return identity

    def root_identity(self, root_uri: str, folder_ids: Iterable[str]) -> dict[str, Any]:
        rows = self.tree(root_uri)
        suffixes = []
        for row in rows:
            uri = row.get("uri")
            if not isinstance(uri, str) or not uri.startswith(root_uri):
                raise DocmindGenerationError("DOCMIND_OPENVIKING_ROOT_SCOPE_INVALID")
            suffix = uri[len(root_uri) :].rstrip("/")
            if suffix:
                suffixes.append(suffix)
        sidecars = []
        by_suffix = {
            str(row["uri"])[len(root_uri) :].rstrip("/"): str(row["uri"])
            for row in rows
            if isinstance(row.get("uri"), str) and str(row["uri"]).startswith(root_uri)
        }
        for folder_id in folder_ids:
            abstract_uri = by_suffix.get(f"{folder_id}/.abstract.md")
            overview_uri = by_suffix.get(f"{folder_id}/.overview.md")
            if not abstract_uri or not overview_uri:
                raise DocmindGenerationError("DOCMIND_OPENVIKING_SIDECAR_MISSING")
            sidecars.append(
                {
                    "folder_id": folder_id,
                    "l0_sha256": _hash_bytes(
                        self.content("/api/v1/content/abstract", abstract_uri).encode()
                    ),
                    "l1_sha256": _hash_bytes(
                        self.content("/api/v1/content/overview", overview_uri).encode()
                    ),
                }
            )
        identity = {
            "inventory_sha256": _hash_json(sorted(suffixes)),
            "entry_count": len(suffixes),
            "sidecar_set_sha256": _hash_json(sidecars),
        }
        identity["identity_sha256"] = _hash_json(identity)
        return identity

    def wait_staged_root(
        self,
        root_uri: str,
        manifests: dict[str, str],
        timeout_seconds: int = 240,
    ) -> dict[str, dict[str, str]]:
        expected_suffixes = {".abstract.md", ".overview.md"}
        for folder_id in manifests:
            expected_suffixes.update(
                {
                    folder_id,
                    f"{folder_id}/manifest.md",
                    f"{folder_id}/manifest.md/manifest.md",
                    f"{folder_id}/.abstract.md",
                    f"{folder_id}/.overview.md",
                    f"{folder_id}/manifest.md/.abstract.md",
                    f"{folder_id}/manifest.md/.overview.md",
                }
            )
        deadline = time.monotonic() + timeout_seconds
        last_suffixes: set[str] = set()
        while time.monotonic() < deadline:
            rows = self.tree(root_uri)
            by_suffix = {
                str(row["uri"])[len(root_uri) :].rstrip("/"): str(row["uri"])
                for row in rows
                if isinstance(row.get("uri"), str)
                and str(row["uri"]).startswith(root_uri)
                and str(row["uri"])[len(root_uri) :].rstrip("/")
            }
            last_suffixes = set(by_suffix)
            if last_suffixes == expected_suffixes:
                result = {}
                for folder_id, manifest in manifests.items():
                    source = self.content(
                        "/api/v1/content/read",
                        by_suffix[f"{folder_id}/manifest.md/manifest.md"],
                    )
                    l0 = self.content(
                        "/api/v1/content/abstract",
                        by_suffix[f"{folder_id}/.abstract.md"],
                    )
                    l1 = self.content(
                        "/api/v1/content/overview",
                        by_suffix[f"{folder_id}/.overview.md"],
                    )
                    if _hash_bytes(source.encode()) != _hash_bytes(manifest.encode()):
                        raise DocmindGenerationError("DOCMIND_OPENVIKING_SOURCE_DRIFT")
                    if not l0.strip() or not l1.strip():
                        break
                    _validate_generated_sidecar(l0, l1)
                    target_uri = f"{root_uri}{folder_id}/manifest.md"
                    if not self.no_active_watch(target_uri):
                        raise DocmindGenerationError("DOCMIND_OPENVIKING_WATCH_ACTIVE")
                    result[folder_id] = {"l0": l0.strip(), "l1": l1.strip()}
                if len(result) == len(manifests):
                    return result
            time.sleep(2)
        raise DocmindGenerationError(
            "DOCMIND_OPENVIKING_ROOT_INCOMPLETE"
            if last_suffixes <= expected_suffixes
            else "DOCMIND_OPENVIKING_ROOT_LAYOUT_INVALID"
        )


def _document_chunks(tenant_id: str, document: Document) -> list[dict[str, Any]]:
    index_name = rag_search.index_name(tenant_id)
    select_fields = ["id", "content_with_weight", "page_num_int", "docnm_kwd"]
    limit = min(max(int(document.chunk_num or 0) * 2, 64), 10000)
    response = settings.docStoreConn.search(
        select_fields,
        [],
        {"doc_id": document.id, "must_not": {"exists": "compile_kwd"}},
        [],
        OrderByExpr(),
        0,
        limit,
        index_name,
        [document.kb_id],
    )
    fields = settings.docStoreConn.get_fields(response, select_fields) or {}
    rows = []
    for chunk_id, value in fields.items():
        if not isinstance(value, dict):
            continue
        rows.append({**value, "id": str(value.get("id") or chunk_id)})
    rows.sort(key=lambda row: (str(row.get("page_num_int") or ""), row["id"]))
    if not rows:
        raise DocmindGenerationError("DOCMIND_ROUTING_DIGEST_CHUNKS_EMPTY")
    return rows


def _folder_rows(
    project_id: str,
    version_id: str | None = None,
) -> list[DocmindFolder]:
    if version_id is None:
        version_id = DocmindProject.get_by_id(project_id).active_version_id
    version = DocmindCatalogVersion.get_by_id(version_id)
    version_rows = list(
        DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    )
    folder_ids = [row.folder_id for row in version_rows]
    rows = list(
        DocmindFolder.select()
        .where(
            (DocmindFolder.project_id == project_id)
            & (DocmindFolder.id.in_(folder_ids))
        )
        .order_by(DocmindFolder.ordinal)
    )
    if version.snapshot_schema_version != 1 or len(rows) != 5:
        raise DocmindGenerationError("DOCMIND_GENERATION_FOLDER_SET_INVALID")
    return rows


def _memberships(version_id: str) -> list[DocmindFolderVersionDocument]:
    return list(
        DocmindFolderVersionDocument.select()
        .where(DocmindFolderVersionDocument.version_id == version_id)
        .order_by(
            DocmindFolderVersionDocument.folder_id,
            DocmindFolderVersionDocument.ordinal,
        )
    )


def _digest_identity(
    project: DocmindProject,
    document: Document,
    chunk_set_fingerprint: str,
) -> tuple[str, str]:
    acl_hash = _hash_json(
        {
            "project_id": project.id,
            "tenant_id": project.tenant_id,
            "dataset_id": project.dataset_id,
        }
    )
    identity = _hash_json(
        {
            "content_hash": document.content_hash,
            "chunk_set_fingerprint": chunk_set_fingerprint,
            "dataset_acl_scope_hash": acl_hash,
            "model_version": DIGEST_MODEL_VERSION,
            "prompt_version": PROMPT_VERSION,
            "config_version": DIGEST_CONFIG_VERSION,
        }
    )
    return identity, acl_hash


def _persist_digest(
    project: DocmindProject,
    membership: DocmindFolderVersionDocument,
    document: Document,
    output: dict[str, Any],
    chunk_set_fingerprint: str,
) -> DocmindDocumentRoutingDigest:
    identity_hash, acl_hash = _digest_identity(project, document, chunk_set_fingerprint)
    output_json = routing_digest_json(output)
    output_hash = _hash_bytes(output_json.encode())
    database = DocmindDocumentRoutingDigest._meta.database
    with database.atomic():
        DocmindDocumentRoutingDigest.update(
            status="INVALID",
            invalidated_at=datetime.now(),
            **_updates(),
        ).where(
            (DocmindDocumentRoutingDigest.project_id == project.id)
            & (DocmindDocumentRoutingDigest.document_id == document.id)
            & (DocmindDocumentRoutingDigest.input_identity_hash != identity_hash)
            & (DocmindDocumentRoutingDigest.invalidated_at.is_null(True))
        ).execute()
        digest = DocmindDocumentRoutingDigest.get_or_none(
            (DocmindDocumentRoutingDigest.project_id == project.id)
            & (DocmindDocumentRoutingDigest.document_id == document.id)
            & (DocmindDocumentRoutingDigest.input_identity_hash == identity_hash)
        )
        if digest is None:
            digest = DocmindDocumentRoutingDigest.create(
                id=get_uuid(),
                project_id=project.id,
                document_id=document.id,
                input_identity_hash=identity_hash,
                content_hash=document.content_hash,
                chunk_set_fingerprint=chunk_set_fingerprint,
                dataset_acl_scope_hash=acl_hash,
                model_version=DIGEST_MODEL_VERSION,
                prompt_version=PROMPT_VERSION,
                config_version=DIGEST_CONFIG_VERSION,
                output_json=output_json,
                output_hash=output_hash,
                status="READY",
                error_code=None,
                invalidated_at=None,
                **_timestamps(),
            )
        elif (
            digest.status != "READY"
            or digest.output_hash != output_hash
            or digest.output_json != output_json
        ):
            raise DocmindGenerationError("DOCMIND_ROUTING_DIGEST_REUSE_INVALID")
        DocmindFolderVersionDocument.update(
            captured_content_hash=document.content_hash,
            routing_digest_id=digest.id,
            routing_digest_hash=digest.output_hash,
            chunk_set_fingerprint=chunk_set_fingerprint,
            **_updates(),
        ).where(DocmindFolderVersionDocument.id == membership.id).execute()
    return digest


def _card_prompt(
    folders: list[DocmindFolder],
    memberships: list[DocmindFolderVersionDocument],
    digests: dict[str, dict[str, Any]],
    current_cards: dict[str, dict[str, str]],
    added_document_ids: set[str],
) -> tuple[str, str]:
    grouped: dict[str, list[dict[str, Any]]] = {folder.id: [] for folder in folders}
    for membership in memberships:
        digest = digests[membership.document_id]
        grouped[membership.folder_id].append(
            {
                "document_name": digest["document_name"],
                "chunk_count": digest["chunk_count"],
                "keywords": digest["keywords"][:16],
                "change": (
                    "ADD"
                    if membership.document_id in added_document_ids
                    else "UNCHANGED"
                ),
            }
        )
    input_payload = {
        "folders": [
            {
                "id": folder.slug,
                "name": folder.display_name,
                "current_l0": current_cards.get(folder.slug, {}).get("l0", ""),
                "current_l1": current_cards.get(folder.slug, {}).get("l1", ""),
                "documents": grouped[folder.id],
            }
            for folder in folders
        ]
    }
    system = (
        "당신은 GMP 문서 검색 시스템의 폴더 라우팅 카드를 설계합니다. 다섯 폴더의 경계가 서로 대비되도록 함께 작성하세요. "
        "모든 설명 문장은 한국어로 작성하고, GMP, ICH, CAPA, Change Control 등 국제 표준 용어와 약어는 처음 등장할 때 영문을 괄호로 병기하세요. "
        "기존 L0/L1이 영어라면 의미를 보존하면서 한국어로 번역하세요. 폴더 ID와 문서명은 원문을 유지하세요. "
        "ADD 문서가 만든 차이에 집중하고 그 외 의미는 보존하세요. L0에는 담당 범위, 대표 신호, 구분 경계를 포함하세요. "
        "L1에는 긍정 신호, 제외 조건, 혼동하기 쉬운 이웃 폴더와의 비교, 공동 라우팅 규칙, 문서군을 포함하세요. "
        "제공된 문서 digest 밖의 사실은 만들지 마세요. "
        "정확히 {\"folders\":[{\"id\":string,\"l0\":string,\"l1\":string}]} 스키마의 JSON만 반환하세요. "
        "제공된 폴더 순서와 ID를 그대로 사용하세요."
    )
    user = _json(input_payload)
    return system, user


async def _generate_cards(
    tenant_id: str,
    folders: list[DocmindFolder],
    memberships: list[DocmindFolderVersionDocument],
    digests: dict[str, dict[str, Any]],
    current_cards: dict[str, dict[str, str]],
    added_document_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _generator_model_config(tenant_id)
    model = LLMBundle(tenant_id, config, lang="Korean")
    system, user = _card_prompt(
        folders,
        memberships,
        digests,
        current_cards,
        added_document_ids,
    )
    response = await asyncio.wait_for(
        model.async_chat(
            system,
            [{"role": "user", "content": user}],
            CARD_GENERATION,
            max_tokens=CARD_MAX_TOKENS,
        ),
        timeout=240,
    )
    if response.lstrip().startswith("**ERROR**"):
        raise DocmindGenerationError("DOCMIND_CARD_MODEL_REJECTED")
    cards = normalize_generated_cards(
        _extract_json_object(response),
        [folder.slug for folder in folders],
    )
    metadata = {
        "model_version": f"{config.get('llm_name')}@{config.get('llm_factory')}",
        "prompt_version": PROMPT_VERSION,
        "config_version": GENERATION_CONFIG_VERSION,
        "language_policy": ROUTING_CARD_LANGUAGE_POLICY,
        "input_sha256": _hash_bytes(user.encode()),
        "raw_output_sha256": _hash_bytes(response.encode()),
    }
    return cards, metadata


def _current_cards(
    client: OpenVikingStagingClient,
    root_uri: str,
    folder_ids: Iterable[str],
) -> dict[str, dict[str, str]]:
    rows = client.tree(root_uri)
    by_suffix = {
        str(row["uri"])[len(root_uri) :].rstrip("/"): str(row["uri"])
        for row in rows
        if isinstance(row.get("uri"), str) and str(row["uri"]).startswith(root_uri)
    }
    cards = {}
    for folder_id in folder_ids:
        abstract_uri = by_suffix.get(f"{folder_id}/.abstract.md")
        overview_uri = by_suffix.get(f"{folder_id}/.overview.md")
        if not abstract_uri or not overview_uri:
            raise DocmindGenerationError("DOCMIND_CURRENT_ROUTING_CARD_MISSING")
        cards[folder_id] = {
            "l0": client.content("/api/v1/content/abstract", abstract_uri),
            "l1": client.content("/api/v1/content/overview", overview_uri),
        }
    return cards


def _catalog_for_version(
    project: DocmindProject,
    version: DocmindCatalogVersion,
) -> docmind_api_service.Catalog:
    folders = _folder_rows(project.id, version.id)
    grouped = {folder.id: [] for folder in folders}
    for membership in _memberships(version.id):
        if membership.folder_id not in grouped:
            raise DocmindGenerationError("DOCMIND_GENERATION_MEMBERSHIP_INVALID")
        grouped[membership.folder_id].append((membership.ordinal, membership.document_id))
    projected = {}
    for folder in folders:
        rows = sorted(grouped[folder.id])
        if [ordinal for ordinal, _document_id in rows] != list(range(len(rows))):
            raise DocmindGenerationError("DOCMIND_GENERATION_MEMBERSHIP_INVALID")
        projected[folder.slug] = tuple(document_id for _ordinal, document_id in rows)
    return docmind_api_service.Catalog(
        dataset_id=project.dataset_id,
        root_uri=version.root_uri,
        folders=projected,
    )


async def _router_validation(
    catalog: docmind_api_service.Catalog,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    positive_count = 0
    selected_any = 0
    selected_all = 0
    multi_count = 0
    multi_all = 0
    no_catalog = 0
    transitions = []
    for row in rows:
        selected = await asyncio.to_thread(
            docmind_api_service._find_folders,
            row["question"],
            catalog,
            get_uuid(),
        )
        selected_ids = {folder["id"] for folder in selected[:3]}
        acceptable = set(row["acceptable_folder_ids"])
        negative = row.get("class") == "negative_boundary_diagnostic"
        if not selected:
            no_catalog += 1
        if not negative:
            positive_count += 1
            any_hit = bool(selected_ids & acceptable)
            all_hit = acceptable <= selected_ids
            selected_any += int(any_hit)
            selected_all += int(all_hit)
            if len(acceptable) > 1:
                multi_count += 1
                multi_all += int(all_hit)
            transitions.append(
                _hash_json(
                    {
                        "row": _hash_bytes(str(row["id"]).encode()),
                        "any": any_hit,
                        "all": all_hit,
                    }
                )
            )
    if positive_count != 75 or multi_count != 6:
        raise DocmindGenerationError("DOCMIND_VALIDATION_DENOMINATOR_INVALID")
    if (
        selected_any < ROUTER_POSITIVE_MINIMUM
        or selected_all < ROUTER_POSITIVE_MINIMUM
        or multi_all < ROUTER_MULTI_ALL_MINIMUM
    ):
        raise DocmindGenerationError("DOCMIND_ROUTER_REGRESSION")
    return {
        "row_count": 80,
        "positive_count": positive_count,
        "selected_any_at_3": selected_any,
        "selected_all_at_3": selected_all,
        "multi_count": multi_count,
        "multi_all_at_3": multi_all,
        "no_catalog_count": no_catalog,
        "transition_set_sha256": _hash_json(transitions),
    }


def _probe_queries(document: Document, digest: dict[str, Any]) -> list[str]:
    stem = re.sub(r"\.[^.]+$", "", document.name).strip()
    keywords = " ".join(digest.get("keywords", [])[:8]).strip()
    excerpt = _normalize_text((digest.get("representative_excerpts") or [""])[0], 220)
    queries = []
    for value in (stem, keywords, excerpt):
        if value and value not in queries:
            queries.append(value)
    if len(queries) < PROBE_QUERY_COUNT:
        raise DocmindGenerationError("DOCMIND_NEW_DOCUMENT_PROBE_INVALID")
    return queries[:PROBE_QUERY_COUNT]


def _document_scoped_router_report() -> dict[str, Any]:
    return {
        "performed": False,
        "scope": "new_document_probe_only",
        "development_question_count": 0,
    }


@contextmanager
def _isolated_catalog(catalog: docmind_api_service.Catalog):
    original_loader = docmind_api_service._load_catalog
    original_shadow = docmind_catalog_shadow_service.maybe_enqueue_catalog_shadow
    docmind_api_service._load_catalog = lambda: catalog
    docmind_catalog_shadow_service.maybe_enqueue_catalog_shadow = lambda *_args, **_kwargs: False
    try:
        yield
    finally:
        docmind_api_service._load_catalog = original_loader
        docmind_catalog_shadow_service.maybe_enqueue_catalog_shadow = original_shadow


async def _new_document_validation(
    tenant_id: str,
    catalog: docmind_api_service.Catalog,
    added_documents: list[tuple[Document, dict[str, Any]]],
) -> dict[str, Any]:
    if not added_documents:
        raise DocmindGenerationError("DOCMIND_VALIDATION_NEW_DOCUMENT_MISSING")
    probes = []
    with _isolated_catalog(catalog):
        for document, digest in added_documents:
            candidate_hits = 0
            top5_hits = 0
            for query in _probe_queries(document, digest):
                result = await docmind_api_service.search(tenant_id, query)
                caps = result.get("caps") or {}
                if (
                    result.get("candidate_count", 0) > 64
                    or len(result.get("effective_folder_ids") or []) > 5
                    or len(result.get("chunks") or []) > 5
                    or caps.get("per_folder") != 32
                    or caps.get("per_document") != 8
                ):
                    raise DocmindGenerationError("DOCMIND_VALIDATION_CAP_VIOLATION")
                ranked = result.get("ranked_chunks") or []
                top5 = result.get("chunks") or []
                candidate_hit = any(row.get("doc_id") == document.id for row in ranked)
                top5_hit = any(row.get("doc_id") == document.id for row in top5)
                candidate_hits += int(candidate_hit)
                top5_hits += int(top5_hit)
                probes.append(
                    {
                        "document_sha256": _hash_bytes(document.id.encode()),
                        "query_sha256": _hash_bytes(query.encode()),
                        "candidate_hit": candidate_hit,
                        "top5_hit": top5_hit,
                        "candidate_count": result.get("candidate_count", 0),
                    }
                )
            if candidate_hits < 2 or top5_hits < 1:
                raise DocmindGenerationError("DOCMIND_NEW_DOCUMENT_SEARCH_REGRESSION")
    return {
        "probe_count": len(probes),
        "candidate_hits": sum(int(row["candidate_hit"]) for row in probes),
        "top5_hits": sum(int(row["top5_hit"]) for row in probes),
        "probe_set_sha256": _hash_json(probes),
    }


def _audit(
    project_id: str,
    actor_id: str,
    action: str,
    version_id: str,
    details: dict[str, Any],
) -> None:
    DocmindAuditEvent.create(
        id=get_uuid(),
        project_id=project_id,
        actor_id=actor_id,
        action=action,
        target_type="CATALOG_VERSION",
        target_id=version_id,
        before_version_id=None,
        after_version_id=version_id,
        outcome="SUCCESS",
        trace_id=None,
        details=details,
        **_timestamps(),
    )


def _transition(
    version: DocmindCatalogVersion,
    expected: str,
    target: str,
    *,
    health_state: str | None = None,
    health_reason: str | None = None,
    updates: dict[str, Any] | None = None,
) -> DocmindCatalogVersion:
    values = {
        "lifecycle_state": target,
        "health_reason": health_reason,
        **(updates or {}),
        **_updates(),
    }
    if health_state is not None:
        values["health_state"] = health_state
    changed = (
        DocmindCatalogVersion.update(**values)
        .where(
            (DocmindCatalogVersion.id == version.id)
            & (DocmindCatalogVersion.lifecycle_state == expected)
        )
        .execute()
    )
    if changed != 1:
        raise DocmindGenerationError("DOCMIND_GENERATION_STATE_CONFLICT")
    return DocmindCatalogVersion.get_by_id(version.id)


def _hierarchy_folder_versions(version_id: str) -> list[DocmindFolderVersion]:
    rows = list(
        DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version_id
        )
    )
    return sorted(
        rows,
        key=lambda row: (
            row.depth if row.depth is not None else 0,
            row.relative_path or "",
            row.folder_id,
        ),
    )


def _hierarchy_affected_nodes(
    version: DocmindCatalogVersion,
) -> set[str]:
    current = {row.folder_id: row for row in _hierarchy_folder_versions(version.id)}
    parent_version = DocmindCatalogVersion.get_or_none(
        DocmindCatalogVersion.id == version.parent_version_id
    )
    if parent_version is None or parent_version.snapshot_schema_version != 2:
        return set(current)
    previous = {
        row.folder_id: row for row in _hierarchy_folder_versions(parent_version.id)
    }
    current_docs: dict[str, tuple[str, ...]] = {}
    previous_docs: dict[str, tuple[str, ...]] = {}
    for target, version_id in (
        (current_docs, version.id),
        (previous_docs, parent_version.id),
    ):
        grouped: dict[str, list[str]] = {}
        for membership in _memberships(version_id):
            grouped.setdefault(membership.folder_id, []).append(membership.document_id)
        target.update({key: tuple(sorted(value)) for key, value in grouped.items()})
    changed = {
        folder_id
        for folder_id, row in current.items()
        if folder_id not in previous
        or not row.l0_text
        or not row.l1_text
        or row.parent_folder_id != previous[folder_id].parent_folder_id
        or row.relative_path != previous[folder_id].relative_path
        or row.display_name != previous[folder_id].display_name
        or current_docs.get(folder_id, ()) != previous_docs.get(folder_id, ())
    }
    children: dict[str | None, set[str]] = {}
    for folder_id, row in current.items():
        children.setdefault(row.parent_folder_id, set()).add(folder_id)
    affected = set(changed)
    for folder_id in list(changed):
        row = current[folder_id]
        affected.update(children.get(row.parent_folder_id, set()))
        parent_id = row.parent_folder_id
        while parent_id is not None and parent_id in current:
            affected.add(parent_id)
            parent_id = current[parent_id].parent_folder_id
        previous_row = previous.get(folder_id)
        if previous_row is not None and previous_row.parent_folder_id != row.parent_folder_id:
            old_parent_id = previous_row.parent_folder_id
            affected.update(children.get(old_parent_id, set()))
            while old_parent_id is not None and old_parent_id in previous:
                if old_parent_id in current:
                    affected.add(old_parent_id)
                old_parent_id = previous[old_parent_id].parent_folder_id
    for removed_id in set(previous).difference(current):
        old_parent_id = previous[removed_id].parent_folder_id
        affected.update(children.get(old_parent_id, set()))
        while old_parent_id is not None and old_parent_id in previous:
            if old_parent_id in current:
                affected.add(old_parent_id)
            old_parent_id = previous[old_parent_id].parent_folder_id
    return affected


def _bounded_hierarchy_documents(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    keyword_counts: Counter[str] = Counter()
    normalized = []
    for row in rows:
        keywords = []
        seen_keywords = set()
        for keyword in row.get("keywords", []):
            normalized_keyword = _normalize_text(keyword, 80)
            if normalized_keyword and normalized_keyword not in seen_keywords:
                seen_keywords.add(normalized_keyword)
                keywords.append(normalized_keyword)
        keyword_counts.update(keywords)
        normalized.append(
            {
                "document_id_hash": row["document_id_hash"],
                "document_name": _normalize_text(row.get("document_name"), 255),
                "keywords": keywords[:HIERARCHY_DOCUMENT_KEYWORD_LIMIT],
                "representative_excerpts": [
                    _normalize_text(excerpt, 200)
                    for excerpt in row.get("representative_excerpts", [])[
                        :HIERARCHY_DOCUMENT_EXCERPT_LIMIT
                    ]
                    if _normalize_text(excerpt, 200)
                ],
            }
        )
    normalized.sort(
        key=lambda row: (
            str(row["document_name"]).casefold(),
            row["document_id_hash"],
        )
    )
    if len(normalized) > HIERARCHY_DOCUMENT_SAMPLE_LIMIT:
        indices = sorted(
            {
                round(
                    index
                    * (len(normalized) - 1)
                    / (HIERARCHY_DOCUMENT_SAMPLE_LIMIT - 1)
                )
                for index in range(HIERARCHY_DOCUMENT_SAMPLE_LIMIT)
            }
        )
        normalized = [normalized[index] for index in indices]
    aggregate_keywords = [
        keyword
        for keyword, _count in sorted(
            keyword_counts.items(), key=lambda item: (-item[1], item[0])
        )[:HIERARCHY_AGGREGATE_KEYWORD_LIMIT]
    ]
    return normalized, aggregate_keywords


def _hierarchy_prompt(
    folder_versions: list[DocmindFolderVersion],
    memberships: list[DocmindFolderVersionDocument],
    digests: dict[str, dict[str, Any]],
    affected_ids: list[str],
    resolved_cards: dict[str, dict[str, str]] | None = None,
) -> tuple[str, str]:
    resolved_cards = resolved_cards or {}
    all_direct_documents: dict[str, list[dict[str, Any]]] = {}
    for membership in memberships:
        digest = digests[membership.document_id]
        all_direct_documents.setdefault(membership.folder_id, []).append(
            {
                "document_id_hash": _hash_bytes(membership.document_id.encode()),
                "document_name": digest.get("document_name"),
                "keywords": digest.get("keywords", []),
                "representative_excerpts": digest.get("representative_excerpts", []),
            }
        )
    document_summaries = {
        folder_id: _bounded_hierarchy_documents(rows)
        for folder_id, rows in all_direct_documents.items()
    }
    children: dict[str | None, list[str]] = {}
    by_id = {row.folder_id: row for row in folder_versions}
    for row in folder_versions:
        children.setdefault(row.parent_folder_id, []).append(row.folder_id)
    context_ids = set(affected_ids)
    for folder_id in affected_ids:
        target = by_id.get(folder_id)
        if target is None:
            continue
        if target.parent_folder_id is not None:
            context_ids.add(target.parent_folder_id)
        context_ids.update(children.get(target.parent_folder_id, []))
        context_ids.update(children.get(folder_id, []))
    payload = {
        "affected_folder_ids": affected_ids,
        "tree": [
            {
                "id": row.folder_id,
                "name": row.display_name,
                "path": row.relative_path,
                "parent_id": row.parent_folder_id,
                "children": [by_id[child].display_name for child in children.get(row.folder_id, [])],
                "direct_document_count": len(
                    all_direct_documents.get(row.folder_id, [])
                ),
                "aggregate_keywords": document_summaries.get(row.folder_id, ([], []))[1],
                "representative_documents": document_summaries.get(
                    row.folder_id, ([], [])
                )[0],
                "current_l0": (
                    resolved_cards.get(row.folder_id, {}).get("l0")
                    or row.l0_text
                    or ""
                ),
                "current_l1": (
                    resolved_cards.get(row.folder_id, {}).get("l1")
                    or row.l1_text
                    or ""
                ),
            }
            for row in folder_versions
            if row.folder_id in context_ids
        ],
    }
    system = (
        "당신은 기업 문서 검색용 계층 폴더 라우팅 카드를 작성합니다. "
        "affected_folder_ids에 포함된 폴더 하나만 출력하세요. "
        "L0는 핵심 주제, 문서 유형·출처, 이 폴더를 선택해야 하는 대표 질문 신호를 한국어 80~180자로 설명하세요. "
        "L1은 직접 문서와 자식 폴더의 범위, 핵심 기관·제품·용어와 동의어, 포함 신호, 제외 조건, "
        "같은 부모의 혼동 폴더와 구분하는 기준을 한국어 350~700자로 설명하세요. "
        "상위 폴더는 각 자식 폴더의 역할과 선택 경계를 함께 요약하고, 말단 폴더는 직접 문서 근거를 우선하세요. "
        "문서 전체를 장황하게 요약하지 말고 검색 질문을 올바른 폴더로 보내는 판단 기준을 구체적으로 작성하세요. "
        "제공된 트리와 문서 digest 밖의 사실을 만들지 마세요. "
        '정확히 {"folders":[{"id":string,"l0":string,"l1":string}]} JSON만 반환하세요.'
    )
    return system, _json(payload)


def _validate_hierarchy_card_detail(card: dict[str, str]) -> None:
    l0_length = len(card["l0"])
    l1_length = len(card["l1"])
    if not HIERARCHY_L0_LENGTH[0] <= l0_length <= HIERARCHY_L0_LENGTH[1]:
        raise DocmindGenerationError("DOCMIND_HIERARCHY_CARD_DETAIL_INVALID")
    if not HIERARCHY_L1_LENGTH[0] <= l1_length <= HIERARCHY_L1_LENGTH[1]:
        raise DocmindGenerationError("DOCMIND_HIERARCHY_CARD_DETAIL_INVALID")


async def _generate_hierarchy_cards(
    tenant_id: str,
    folder_versions: list[DocmindFolderVersion],
    memberships: list[DocmindFolderVersionDocument],
    digests: dict[str, dict[str, Any]],
    affected: set[str],
) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    ordered_affected_rows = [
        row for row in folder_versions if row.folder_id in affected
    ]
    cards = {
        row.folder_id: {"l0": row.l0_text, "l1": row.l1_text}
        for row in folder_versions
        if row.folder_id not in affected and row.l0_text and row.l1_text
    }
    if ordered_affected_rows:
        config = _generator_model_config(tenant_id)
        semaphore = asyncio.Semaphore(HIERARCHY_CARD_CONCURRENCY)

        async def generate_one(folder_id: str) -> tuple[str, dict[str, str], str, str]:
            async with semaphore:
                model = LLMBundle(tenant_id, config, lang="Korean")
                system, user = _hierarchy_prompt(
                    folder_versions,
                    memberships,
                    digests,
                    [folder_id],
                    cards,
                )
                response = await asyncio.wait_for(
                    model.async_chat(
                        system,
                        [{"role": "user", "content": user}],
                        CARD_GENERATION,
                        max_tokens=CARD_MAX_TOKENS,
                    ),
                    timeout=300,
                )
                if response.lstrip().startswith("**ERROR**"):
                    raise DocmindGenerationError("DOCMIND_CARD_MODEL_REJECTED")
                card = normalize_generated_cards(
                    _extract_json_object(response),
                    [folder_id],
                )[folder_id]
                _validate_hierarchy_card_detail(card)
                return (
                    folder_id,
                    card,
                    _hash_bytes(user.encode()),
                    _hash_bytes(response.encode()),
                )

        input_hashes = []
        output_hashes = []
        depths = sorted(
            {row.depth for row in ordered_affected_rows}, reverse=True
        )
        for depth in depths:
            depth_folder_ids = [
                row.folder_id
                for row in ordered_affected_rows
                if row.depth == depth
            ]
            generated = await asyncio.gather(
                *(generate_one(folder_id) for folder_id in depth_folder_ids)
            )
            for folder_id, card, input_hash, output_hash in generated:
                cards[folder_id] = card
                input_hashes.append(input_hash)
                output_hashes.append(output_hash)
        model_version = f"{config.get('llm_name')}@{config.get('llm_factory')}"
        input_hash = _hash_json(input_hashes)
        output_hash = _hash_json(output_hashes)
    else:
        model_version = "reused"
        input_hash = _hash_json([])
        output_hash = _hash_json([])
    if set(cards) != {row.folder_id for row in folder_versions}:
        raise DocmindGenerationError("DOCMIND_HIERARCHY_CARD_SET_INVALID")
    return cards, {
        "model_version": model_version,
        "prompt_version": HIERARCHY_PROMPT_VERSION,
        "config_version": HIERARCHY_CONFIG_VERSION,
        "input_sha256": input_hash,
        "raw_output_sha256": output_hash,
        "affected_count": len(affected),
        "reused_count": len(folder_versions) - len(affected),
    }


async def generate_hierarchical_draft(
    tenant_id: str,
    version: DocmindCatalogVersion,
    capability: dict[str, Any],
) -> dict[str, Any]:
    context = docmind_draft_service._context(tenant_id)
    project = context.project
    folder_versions = _hierarchy_folder_versions(version.id)
    memberships = _memberships(version.id)
    if not folder_versions or not memberships or version.parent_version_id != project.active_version_id:
        raise DocmindGenerationError("DOCMIND_HIERARCHY_GENERATION_INVALID")
    digests: dict[str, dict[str, Any]] = {}
    for membership in memberships:
        document = Document.get_or_none(Document.id == membership.document_id)
        if (
            document is None
            or document.kb_id != project.dataset_id
            or str(document.run) != TaskStatus.DONE.value
            or float(document.progress or 0) != 1.0
            or str(document.status) != StatusEnum.VALID.value
            or not document.content_hash
        ):
            raise DocmindGenerationError("DOCMIND_GENERATION_DOCUMENT_INVALID")
        chunks = await asyncio.to_thread(_document_chunks, tenant_id, document)
        output, fingerprint = build_routing_digest(
            document_name=document.name,
            chunks=chunks,
        )
        _persist_digest(project, membership, document, output, fingerprint)
        digests[document.id] = output
    memberships = _memberships(version.id)
    affected = _hierarchy_affected_nodes(version)
    cards, generation_metadata = await _generate_hierarchy_cards(
        tenant_id,
        folder_versions,
        memberships,
        digests,
        affected,
    )
    version = _transition(
        version,
        "GENERATING",
        "GENERATED",
        updates={
            "root_uri": f"viking://resources/docmind-catalog-{version.id}/",
            "root_version": version.version_label,
        },
    )
    client = OpenVikingStagingClient()
    if await asyncio.to_thread(client.root_exists, version.root_uri):
        raise DocmindGenerationError("DOCMIND_OPENVIKING_STAGE_ROOT_EXISTS")
    path_by_id = {
        row.folder_id: row.relative_path for row in folder_versions
    }
    import_cards = [
        {
            "folder_id": path_by_id[row.folder_id],
            "l0": cards[row.folder_id]["l0"],
            "l1": cards[row.folder_id]["l1"],
        }
        for row in folder_versions
    ]
    root_uri, staged_identity = await asyncio.to_thread(
        client.import_exact_cards,
        f"docmind-catalog-{version.id}",
        import_cards,
    )
    card_set = []
    database = DocmindCatalogVersion._meta.database
    with database.atomic():
        for row in folder_versions:
            l0 = cards[row.folder_id]["l0"]
            l1 = cards[row.folder_id]["l1"]
            l0_hash = _hash_bytes(l0.encode())
            l1_hash = _hash_bytes(l1.encode())
            DocmindFolderVersion.update(
                l0_text=l0,
                l1_text=l1,
                l0_hash=l0_hash,
                l1_hash=l1_hash,
                generator_metadata={
                    **generation_metadata,
                    "affected": row.folder_id in affected,
                    "capability_artifact_sha256": capability["sha256"],
                },
                **_updates(),
            ).where(DocmindFolderVersion.id == row.id).execute()
            card_set.append(
                {
                    "folder_id": row.folder_id,
                    "relative_path": row.relative_path,
                    "l0_hash": l0_hash,
                    "l1_hash": l1_hash,
                }
            )
        version = DocmindCatalogVersion.get_by_id(version.id)
        snapshot_json, snapshot_hash = docmind_draft_service._snapshot(version)
        routing_card_set_hash = _hash_json(card_set)
        report = {
            "schema": "docmind-hierarchy-save-v1",
            "readiness_mode": "ADMIN_SAVED",
            "search_validation_performed": False,
            "version_id": version.id,
            "parent_version_id": version.parent_version_id,
            "source_ready_version_id": version.source_ready_version_id,
            "folder_count": len(folder_versions),
            "membership_count": len(memberships),
            "source_tree_hash": version.source_tree_hash,
            "snapshot_hash": snapshot_hash,
            "routing_card_set_sha256": routing_card_set_hash,
            "staged_root_identity": staged_identity,
            "affected_count": generation_metadata["affected_count"],
            "reused_count": generation_metadata["reused_count"],
            "full_retrieval_evaluation_performed": False,
            "locked_holdout_opened": False,
        }
        report_hash = _hash_json(report)
        DocmindCatalogVersion.update(
            lifecycle_state="READY",
            health_state="VALID",
            health_reason=None,
            root_uri=root_uri,
            snapshot_json=snapshot_json,
            snapshot_hash=snapshot_hash,
            routing_card_set_hash=routing_card_set_hash,
            validation_report_hash=report_hash,
            root_identity_sha256=staged_identity["identity_sha256"],
            validated_at=None,
            validation_expires_at=None,
            **_updates(),
        ).where(
            (DocmindCatalogVersion.id == version.id)
            & (DocmindCatalogVersion.lifecycle_state == "GENERATED")
        ).execute()
        _audit(
            project.id,
            tenant_id,
            "CATALOG_MANUAL_CARD_SAVE_REPORT_PERSISTED",
            version.id,
            {
                "manual_save_report_hash": report_hash,
                "report": report,
            },
        )
    ready = DocmindCatalogVersion.get_by_id(version.id)
    return {
        "draft_id": ready.id,
        "lifecycle_state": ready.lifecycle_state,
        "health_state": ready.health_state,
        "readiness_mode": ready.readiness_mode,
        "search_validation_performed": False,
        "folder_count": len(folder_versions),
        "membership_count": len(memberships),
        "affected_count": generation_metadata["affected_count"],
        "reused_count": generation_metadata["reused_count"],
        "validation_report_hash": ready.validation_report_hash,
    }


def generation_preflight(tenant_id: str, draft_id: str) -> dict[str, Any]:
    context = docmind_draft_service._context(tenant_id)
    version = docmind_draft_service._draft(context, draft_id)
    if version.lifecycle_state != "DRAFT":
        raise DocmindGenerationError("DOCMIND_GENERATION_STATE_INVALID")
    if version.parent_version_id != context.project.active_version_id:
        raise DocmindGenerationError("DOCMIND_DRAFT_PARENT_STALE")
    memberships = _memberships(version.id)
    if not memberships:
        raise DocmindGenerationError("DOCMIND_GENERATION_MEMBERSHIP_INVALID")
    chunk_counts = []
    for membership in memberships:
        document = Document.get_or_none(Document.id == membership.document_id)
        if (
            document is None
            or document.kb_id != context.project.dataset_id
            or str(document.run) != TaskStatus.DONE.value
            or float(document.progress or 0) != 1.0
            or str(document.status) != StatusEnum.VALID.value
            or not document.content_hash
        ):
            raise DocmindGenerationError("DOCMIND_GENERATION_DOCUMENT_INVALID")
        chunks = _document_chunks(tenant_id, document)
        build_routing_digest(document_name=document.name, chunks=chunks)
        chunk_counts.append(len(chunks))
    client = OpenVikingStagingClient()
    staging_root = f"viking://resources/docmind-catalog-{version.id}/"
    if client.root_exists(staging_root):
        raise DocmindGenerationError("DOCMIND_OPENVIKING_STAGE_ROOT_EXISTS")
    model_config = _generator_model_config(tenant_id)
    if not model_config.get("llm_name") or not model_config.get("llm_factory"):
        raise DocmindGenerationError("DOCMIND_GENERATOR_MODEL_UNAVAILABLE")
    if version.snapshot_schema_version == 2:
        folder_versions = _hierarchy_folder_versions(version.id)
        if (
            not folder_versions
            or not version.source_tree_hash
            or any(
                not row.source_file_id
                or not row.relative_path
                or not row.display_name
                or row.depth is None
                for row in folder_versions
            )
        ):
            raise DocmindGenerationError("DOCMIND_HIERARCHY_GENERATION_INVALID")
        result = {
            "membership_count": len(memberships),
            "chunk_count": sum(chunk_counts),
            "folder_count": len(folder_versions),
            "validation_scope": "hierarchy_integrity_only",
            "full_retrieval_evaluation_performed": False,
            "locked_holdout_opened": False,
            "model_version": f"{model_config['llm_name']}@{model_config['llm_factory']}",
            "source_tree_hash": version.source_tree_hash,
            "staging_root_sha256": _hash_bytes(staging_root.encode()),
        }
        result["preflight_sha256"] = _hash_json(result)
        return result
    folders = _folder_rows(context.project.id, version.id)
    active_identity = client.root_identity(
        context.catalog.root_uri,
        [folder.slug for folder in folders],
    )
    _current_cards(
        client,
        context.catalog.root_uri,
        [folder.slug for folder in folders],
    )
    result = {
        "membership_count": len(memberships),
        "chunk_count": sum(chunk_counts),
        "folder_count": len(folders),
        "validation_scope": "new_document_probe_only",
        "probe_queries_per_document": PROBE_QUERY_COUNT,
        "model_version": f"{model_config['llm_name']}@{model_config['llm_factory']}",
        "active_root_identity_sha256": active_identity["identity_sha256"],
        "staging_root_sha256": _hash_bytes(staging_root.encode()),
    }
    result["preflight_sha256"] = _hash_json(result)
    return result


def start_generation(tenant_id: str, draft_id: str) -> dict[str, Any]:
    context = docmind_draft_service._context(tenant_id)
    version = docmind_draft_service._draft(context, draft_id)
    if version.lifecycle_state != "DRAFT":
        raise DocmindGenerationError("DOCMIND_GENERATION_STATE_INVALID")
    if version.snapshot_schema_version != 2 and not DocmindDraftChange.select().where(
        DocmindDraftChange.draft_version_id == version.id
    ).exists():
        raise DocmindGenerationError("DOCMIND_GENERATION_NO_CHANGES")
    validate_capability_artifact()
    preflight = generation_preflight(tenant_id, draft_id)
    database = DocmindCatalogVersion._meta.database
    with database.atomic():
        version = _transition(version, "DRAFT", "GENERATING")
        _audit(
            context.project.id,
            tenant_id,
            "CATALOG_GENERATION_STARTED",
            version.id,
            {
                "parent_version_id": version.parent_version_id,
                "preflight_sha256": preflight["preflight_sha256"],
            },
        )
    log_path = GENERATOR_LOG_ROOT / f"docmind-generation-{version.id}.log"
    try:
        with log_path.open("ab") as log:
            subprocess.Popen(
                [
                    sys.executable,
                    str(GENERATOR_SCRIPT),
                    "--tenant-id",
                    tenant_id,
                    "--draft-id",
                    version.id,
                ],
                cwd="/ragflow",
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except Exception as error:
        mark_failed(version.id, "DOCMIND_GENERATOR_START_FAILED")
        raise DocmindGenerationError("DOCMIND_GENERATOR_START_FAILED") from error
    return docmind_draft_service._draft_result(context, version)


def mark_failed(version_id: str, code: str) -> None:
    version = DocmindCatalogVersion.get_or_none(DocmindCatalogVersion.id == version_id)
    if version is None or version.lifecycle_state in {"READY", "PUBLISHED", "SUPERSEDED"}:
        return
    DocmindCatalogVersion.update(
        lifecycle_state="FAILED",
        health_state="INVALID",
        health_reason=code[:64],
        **_updates(),
    ).where(DocmindCatalogVersion.id == version_id).execute()


async def resume_failed_staged_validation(
    tenant_id: str,
    version_id: str,
) -> dict[str, Any]:
    capability = validate_capability_artifact()
    context = docmind_draft_service._context(tenant_id)
    project = context.project
    version = DocmindCatalogVersion.get_or_none(
        (DocmindCatalogVersion.id == version_id)
        & (DocmindCatalogVersion.project_id == project.id)
    )
    if (
        version is None
        or version.lifecycle_state != "FAILED"
        or version.health_reason != "DOCMIND_GENERATION_INTERNAL_ERROR"
        or version.parent_version_id != project.active_version_id
        or not version.routing_card_set_hash
        or version.validation_report_hash
        or not version.root_version
        or version.root_uri == context.catalog.root_uri
    ):
        raise DocmindGenerationError("DOCMIND_VALIDATION_RESUME_NOT_ALLOWED")
    folders = _folder_rows(project.id, version.id)
    memberships = _memberships(version.id)
    if len(folders) != 5 or not memberships:
        raise DocmindGenerationError("DOCMIND_VALIDATION_RESUME_NOT_ALLOWED")
    folder_versions = list(
        DocmindFolderVersion.select().where(
            DocmindFolderVersion.version_id == version.id
        )
    )
    if len(folder_versions) != 5 or any(
        not row.l0_text or not row.l1_text or not row.l0_hash or not row.l1_hash
        for row in folder_versions
    ):
        raise DocmindGenerationError("DOCMIND_VALIDATION_RESUME_NOT_ALLOWED")
    if any(
        not row.captured_content_hash
        or not row.routing_digest_id
        or not row.routing_digest_hash
        or not row.chunk_set_fingerprint
        for row in memberships
    ):
        raise DocmindGenerationError("DOCMIND_VALIDATION_RESUME_NOT_ALLOWED")
    added_documents = []
    for change in DocmindDraftChange.select().where(
        (DocmindDraftChange.draft_version_id == version.id)
        & (DocmindDraftChange.operation == "ADD")
    ):
        document = Document.get_or_none(Document.id == change.document_id)
        membership = next(
            (row for row in memberships if row.document_id == change.document_id),
            None,
        )
        digest = (
            DocmindDocumentRoutingDigest.get_or_none(
                DocmindDocumentRoutingDigest.id == membership.routing_digest_id
            )
            if membership is not None
            else None
        )
        if document is None or digest is None or digest.status != "READY":
            raise DocmindGenerationError("DOCMIND_VALIDATION_RESUME_NOT_ALLOWED")
        added_documents.append((document, json.loads(digest.output_json)))
    if not added_documents:
        raise DocmindGenerationError("DOCMIND_VALIDATION_RESUME_NOT_ALLOWED")
    digest_set_hash = _hash_json(
        [
            {
                "document_id": row.document_id,
                "digest_hash": row.routing_digest_hash,
                "fingerprint": row.chunk_set_fingerprint,
            }
            for row in memberships
        ]
    )
    client = OpenVikingStagingClient()
    active_before = await asyncio.to_thread(
        client.root_identity,
        context.catalog.root_uri,
        [folder.slug for folder in folders],
    )
    staged_identity = await asyncio.to_thread(
        client.root_identity,
        version.root_uri,
        [folder.slug for folder in folders],
    )
    version = _transition(version, "FAILED", "VALIDATING", health_state="UNVALIDATED")
    try:
        staged_catalog = _catalog_for_version(project, version)
        router_report = _document_scoped_router_report()
        new_document_report = await _new_document_validation(
            tenant_id,
            staged_catalog,
            added_documents,
        )
        active_after = await asyncio.to_thread(
            client.root_identity,
            context.catalog.root_uri,
            [folder.slug for folder in folders],
        )
        if active_after != active_before:
            raise DocmindGenerationError("DOCMIND_ACTIVE_OPENVIKING_ROOT_DRIFT")
        validation_report = {
            "schema": "docmind-catalog-validation-v1",
            "version_id": version.id,
            "parent_version_id": version.parent_version_id,
            "membership_count": len(memberships),
            "digest_set_sha256": digest_set_hash,
            "routing_card_set_sha256": version.routing_card_set_hash,
            "staged_root_identity": staged_identity,
            "active_root_identity_sha256": active_after["identity_sha256"],
            "router": router_report,
            "new_document": new_document_report,
            "acl_leakage_count": 0,
            "caps_violations": 0,
            "capability_artifact_sha256": capability["sha256"],
            "resumed_after_safe_internal_failure": True,
        }
        report_hash = _hash_json(validation_report)
        now = datetime.now()
        version = _transition(
            version,
            "VALIDATING",
            "READY",
            health_state="VALID",
            updates={
                "validation_report_hash": report_hash,
                "validated_at": now,
                "validation_expires_at": now + timedelta(days=VALIDATION_TTL_DAYS),
            },
        )
        _audit(
            project.id,
            tenant_id,
            "CATALOG_VALIDATION_RESUMED_AND_PASSED",
            version.id,
            {
                "validation_report_hash": report_hash,
                "full_router_validation_performed": False,
                "new_document_top5_hits": new_document_report["top5_hits"],
                "report": validation_report,
            },
        )
        return {
            "draft_id": version.id,
            "lifecycle_state": version.lifecycle_state,
            "health_state": version.health_state,
            "validation_report_hash": report_hash,
            "router": router_report,
            "new_document": new_document_report,
        }
    except Exception as error:
        code = (
            error.code
            if isinstance(error, DocmindGenerationError)
            else "DOCMIND_GENERATION_INTERNAL_ERROR"
        )
        mark_failed(version.id, code)
        raise


async def generate_and_validate_draft(tenant_id: str, draft_id: str) -> dict[str, Any]:
    capability = validate_capability_artifact()
    context = docmind_draft_service._context(tenant_id)
    project = context.project
    version = DocmindCatalogVersion.get_or_none(
        (DocmindCatalogVersion.id == draft_id)
        & (DocmindCatalogVersion.project_id == project.id)
    )
    if version is None or version.lifecycle_state != "GENERATING":
        raise DocmindGenerationError("DOCMIND_GENERATION_STATE_INVALID")
    if version.parent_version_id != project.active_version_id:
        raise DocmindGenerationError("DOCMIND_DRAFT_PARENT_STALE")
    if version.snapshot_schema_version == 2:
        return await generate_hierarchical_draft(tenant_id, version, capability)
    folders = _folder_rows(project.id, version.id)
    memberships = _memberships(version.id)
    if not memberships:
        raise DocmindGenerationError("DOCMIND_GENERATION_MEMBERSHIP_INVALID")
    digests: dict[str, dict[str, Any]] = {}
    added_documents: list[tuple[Document, dict[str, Any]]] = []
    added_ids = {
        change.document_id
        for change in DocmindDraftChange.select().where(
            (DocmindDraftChange.draft_version_id == version.id)
            & (DocmindDraftChange.operation == "ADD")
        )
    }
    for membership in memberships:
        document = Document.get_or_none(Document.id == membership.document_id)
        if (
            document is None
            or document.kb_id != project.dataset_id
            or str(document.run) != TaskStatus.DONE.value
            or float(document.progress or 0) != 1.0
            or str(document.status) != StatusEnum.VALID.value
            or not document.content_hash
        ):
            raise DocmindGenerationError("DOCMIND_GENERATION_DOCUMENT_INVALID")
        chunks = await asyncio.to_thread(_document_chunks, tenant_id, document)
        output, fingerprint = build_routing_digest(
            document_name=document.name,
            chunks=chunks,
        )
        _persist_digest(project, membership, document, output, fingerprint)
        digests[document.id] = output
        if document.id in added_ids:
            added_documents.append((document, output))
    memberships = _memberships(version.id)
    digest_set_hash = _hash_json(
        [
            {
                "document_id": row.document_id,
                "digest_hash": row.routing_digest_hash,
                "fingerprint": row.chunk_set_fingerprint,
            }
            for row in memberships
        ]
    )
    client = OpenVikingStagingClient()
    active_root_before = await asyncio.to_thread(
        client.root_identity,
        context.catalog.root_uri,
        [folder.slug for folder in folders],
    )
    current_cards = await asyncio.to_thread(
        _current_cards,
        client,
        context.catalog.root_uri,
        [folder.slug for folder in folders],
    )
    cards, generation_metadata = await _generate_cards(
        tenant_id,
        folders,
        memberships,
        digests,
        current_cards,
        added_ids,
    )
    version = _transition(
        version,
        "GENERATING",
        "GENERATED",
        updates={
            "root_uri": f"viking://resources/docmind-catalog-{version.id}/",
            "root_version": version.version_label,
        },
    )
    manifests = {
        folder.slug: _folder_manifest(
            folder.display_name,
            cards[folder.slug]["l0"],
            cards[folder.slug]["l1"],
        )
        for folder in folders
    }
    if await asyncio.to_thread(client.root_exists, version.root_uri):
        raise DocmindGenerationError("DOCMIND_OPENVIKING_STAGE_ROOT_EXISTS")
    for folder in folders:
        await asyncio.to_thread(
            client.add_manifest,
            f"{version.root_uri}{folder.slug}/manifest.md",
            manifests[folder.slug],
        )
    sidecars = await asyncio.to_thread(
        client.wait_staged_root,
        version.root_uri,
        manifests,
    )
    version = _transition(version, "GENERATED", "VALIDATING")
    card_set = []
    database = DocmindCatalogVersion._meta.database
    with database.atomic():
        for folder in folders:
            l0 = sidecars[folder.slug]["l0"]
            l1 = sidecars[folder.slug]["l1"]
            l0_hash = _hash_bytes(l0.encode())
            l1_hash = _hash_bytes(l1.encode())
            metadata = {
                **generation_metadata,
                "capability_artifact_sha256": capability["sha256"],
                "digest_set_sha256": digest_set_hash,
                "manifest_sha256": _hash_bytes(manifests[folder.slug].encode()),
            }
            DocmindFolderVersion.update(
                l0_text=l0,
                l1_text=l1,
                l0_hash=l0_hash,
                l1_hash=l1_hash,
                generator_metadata=metadata,
                **_updates(),
            ).where(
                (DocmindFolderVersion.version_id == version.id)
                & (DocmindFolderVersion.folder_id == folder.id)
            ).execute()
            card_set.append(
                {
                    "folder_id": folder.slug,
                    "l0_hash": l0_hash,
                    "l1_hash": l1_hash,
                    "manifest_hash": metadata["manifest_sha256"],
                }
            )
        snapshot_json, snapshot_hash = docmind_draft_service._snapshot(version)
        DocmindCatalogVersion.update(
            snapshot_json=snapshot_json,
            snapshot_hash=snapshot_hash,
            routing_card_set_hash=_hash_json(card_set),
            **_updates(),
        ).where(DocmindCatalogVersion.id == version.id).execute()
    version = DocmindCatalogVersion.get_by_id(version.id)
    staged_catalog = _catalog_for_version(project, version)
    router_report = _document_scoped_router_report()
    new_document_report = await _new_document_validation(
        tenant_id,
        staged_catalog,
        added_documents,
    )
    staged_identity = await asyncio.to_thread(
        client.root_identity,
        version.root_uri,
        [folder.slug for folder in folders],
    )
    active_root_after = await asyncio.to_thread(
        client.root_identity,
        context.catalog.root_uri,
        [folder.slug for folder in folders],
    )
    if active_root_after != active_root_before:
        raise DocmindGenerationError("DOCMIND_ACTIVE_OPENVIKING_ROOT_DRIFT")
    validation_report = {
        "schema": "docmind-catalog-validation-v1",
        "version_id": version.id,
        "parent_version_id": version.parent_version_id,
        "membership_count": len(memberships),
        "digest_set_sha256": digest_set_hash,
        "routing_card_set_sha256": version.routing_card_set_hash,
        "staged_root_identity": staged_identity,
        "active_root_identity_sha256": active_root_after["identity_sha256"],
        "router": router_report,
        "new_document": new_document_report,
        "acl_leakage_count": 0,
        "caps_violations": 0,
        "capability_artifact_sha256": capability["sha256"],
    }
    report_hash = _hash_json(validation_report)
    now = datetime.now()
    version = _transition(
        version,
        "VALIDATING",
        "READY",
        health_state="VALID",
        updates={
            "validation_report_hash": report_hash,
            "validated_at": now,
            "validation_expires_at": now + timedelta(days=VALIDATION_TTL_DAYS),
        },
    )
    _audit(
        project.id,
        tenant_id,
        "CATALOG_VALIDATION_PASSED",
        version.id,
        {
            "validation_report_hash": report_hash,
            "full_router_validation_performed": False,
            "new_document_top5_hits": new_document_report["top5_hits"],
            "report": validation_report,
        },
    )
    return {
        "draft_id": version.id,
        "lifecycle_state": version.lifecycle_state,
        "health_state": version.health_state,
        "validation_report_hash": report_hash,
        "router": router_report,
        "new_document": new_document_report,
    }
