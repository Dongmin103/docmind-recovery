import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from api.apps.services import dataset_api_service
from api.db.joint_services.tenant_model_service import get_model_config_from_provider_instance
from api.db.services import docmind_catalog_service
from api.db.services.docmind_document_path_service import document_relative_paths
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from common.constants import LLMType

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).with_name("docmind_catalog.json")
_LANE_LIMIT = 128
_RRF_K = 60
_DOCUMENT_CANDIDATE_LIMIT = 8
_RERANK_CANDIDATE_LIMIT = 128
_RERANK_TEXT_LIMIT = 2400
_FINAL_RESULT_LIMIT = 5
_DENSE_K = 2048
_DENSE_NUM_CANDIDATES = 4096
_DENSE_SIMILARITY = 0.0
_DEFAULT_RERANK_ID = "jina-reranker-v3.5@jina@Jina"
_RECALL_TIMEOUT_SECONDS = 60
_RERANK_TIMEOUT_SECONDS = 45
_HEX32_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class Catalog:
    dataset_id: str
    root_uri: str
    folders: dict[str, tuple[str, ...]]
    source: str = "static"
    version_id: str | None = None
    folder_tree: tuple[dict[str, Any], ...] = ()


class DocmindCatalogNotInitializedError(RuntimeError):
    """Raised when a database-primary workspace has no active serving catalog."""

    code = "DOCMIND_CATALOG_NOT_INITIALIZED"


@dataclass(frozen=True)
class Candidate:
    folder_id: str
    chunk: dict[str, Any]
    rerank_text: str
    rrf_score: float = 0.0
    bm25_rank: int | None = None
    dense_rank: int | None = None


@dataclass(frozen=True)
class ResolvedScope:
    mode: str
    folder_ids: tuple[str, ...]
    document_ids: tuple[str, ...]


def _load_static_catalog() -> Catalog:
    def reject_duplicate_keys(pairs):
        parsed = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError(f"DocMind catalog contains duplicate JSON key: {key}")
            parsed[key] = value
        return parsed

    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    if not isinstance(raw, dict) or set(raw) != {"dataset_id", "root_uri", "folders"}:
        raise ValueError("DocMind catalog contains unknown or missing fields")
    dataset_id = str(raw.get("dataset_id") or "")
    root_uri = str(raw.get("root_uri") or "")
    raw_folders = raw.get("folders")
    if not _HEX32_PATTERN.fullmatch(dataset_id):
        raise ValueError("DocMind catalog dataset_id must be a lowercase 32-character hexadecimal id")
    if not root_uri:
        raise ValueError("DocMind catalog root_uri is required")
    if not isinstance(raw_folders, list) or not raw_folders:
        raise ValueError("DocMind catalog must contain at least one folder")

    folders: dict[str, tuple[str, ...]] = {}
    seen_documents: set[str] = set()
    for folder in raw_folders:
        if not isinstance(folder, dict) or set(folder) != {"id", "doc_ids"}:
            raise ValueError("DocMind catalog folder contains unknown or missing fields")
        folder_id = str(folder.get("id") or "")
        raw_doc_ids = folder.get("doc_ids")
        if not isinstance(raw_doc_ids, list):
            raise TypeError("DocMind catalog folder doc_ids must be an array")
        doc_ids = tuple(str(doc_id) for doc_id in raw_doc_ids)
        if not _SLUG_PATTERN.fullmatch(folder_id) or folder_id in folders or len(set(doc_ids)) != len(doc_ids):
            raise ValueError("DocMind catalog contains an invalid or duplicate folder")
        if any(not _HEX32_PATTERN.fullmatch(doc_id) or doc_id in seen_documents for doc_id in doc_ids):
            raise ValueError("DocMind catalog contains an invalid or cross-folder duplicate document")
        seen_documents.update(doc_ids)
        folders[folder_id] = doc_ids
    return Catalog(dataset_id=dataset_id, root_uri=root_uri, folders=folders)


def _load_catalog() -> Catalog:
    if os.environ.get("DOCMIND_EMERGENCY_STATIC_FALLBACK") == "1":
        return _load_static_catalog()
    if os.environ.get("DOCMIND_CATALOG_DB_PRIMARY_ENABLED") != "1":
        return _load_static_catalog()

    loaded = docmind_catalog_service.load_default_database_serving_catalog()
    if loaded is None:
        try:
            static_catalog = _load_static_catalog()
        except FileNotFoundError as error:
            raise DocmindCatalogNotInitializedError(DocmindCatalogNotInitializedError.code) from error
        loaded = docmind_catalog_service.load_database_serving_catalog(static_catalog.dataset_id)
        if loaded is None:
            return static_catalog
    return Catalog(
        dataset_id=loaded.dataset_id,
        root_uri=loaded.root_uri,
        folders={key: tuple(value) for key, value in loaded.folders.items()},
        source="database",
        version_id=loaded.active_version_id,
        folder_tree=tuple(dict(row) for row in getattr(loaded, "folder_tree", ())),
    )


def _catalog_version_id(catalog: Catalog) -> str:
    if catalog.version_id:
        return catalog.version_id
    snapshot: dict[str, Any] = {
        "dataset_id": catalog.dataset_id,
        "folders": [
            {"id": folder_id, "doc_ids": list(doc_ids)}
            for folder_id, doc_ids in catalog.folders.items()
        ],
    }
    if catalog.folder_tree:
        snapshot["folder_tree"] = list(catalog.folder_tree)
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"static-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _folder_display_name(folder_id: str) -> str:
    return " ".join(part.capitalize() for part in folder_id.split("-"))


def _tree_by_id(catalog: Catalog) -> dict[str, dict[str, Any]]:
    return {str(row["id"]): row for row in catalog.folder_tree}


def _subtree_ids(catalog: Catalog, folder_id: str) -> list[str]:
    rows = _tree_by_id(catalog)
    valid_ids = set(rows) if rows else set(catalog.folders)
    if folder_id not in valid_ids:
        raise ValueError("DOCMIND_INVALID_FOLDER: folder_ids contains an unknown folder")
    if not rows:
        return [folder_id]

    children: dict[str | None, list[str]] = {}
    for row in catalog.folder_tree:
        children.setdefault(row.get("parent_id"), []).append(str(row["id"]))
    for child_ids in children.values():
        child_ids.sort(
            key=lambda node_id: (
                int(rows[node_id].get("ordinal") or 0),
                str(rows[node_id].get("relative_path") or ""),
                node_id,
            )
        )

    result: list[str] = []

    def visit(node_id: str) -> None:
        result.append(node_id)
        for child_id in children.get(node_id, []):
            visit(child_id)

    visit(folder_id)
    return result


def _document_options(catalog: Catalog) -> list[dict[str, str]]:
    folder_by_document = {
        document_id: folder_id
        for folder_id, document_ids in catalog.folders.items()
        for document_id in document_ids
    }
    document_ids = list(folder_by_document)
    if not document_ids:
        return []

    rows = {str(row.id): row for row in DocumentService.get_by_ids(document_ids)}
    relative_paths = document_relative_paths(catalog.dataset_id, set(document_ids))
    options: list[dict[str, str]] = []
    for document_id in document_ids:
        row = rows.get(document_id)
        relative_path = relative_paths.get(document_id, "")
        name = str(getattr(row, "name", "") or relative_path.rsplit("/", 1)[-1] or document_id)
        options.append(
            {
                "id": document_id,
                "name": name,
                "folder_id": folder_by_document[document_id],
                "relative_path": relative_path or name,
            }
        )
    return options


def list_folders(tenant_id: str) -> dict[str, Any]:
    catalog = _load_catalog()
    if not KnowledgebaseService.accessible(catalog.dataset_id, tenant_id):
        raise PermissionError("DocMind catalog dataset is not accessible")
    if catalog.folder_tree:
        return {
            "dataset_id": catalog.dataset_id,
            "catalog_source": catalog.source,
            "catalog_version_id": _catalog_version_id(catalog),
            "hierarchical": True,
            "documents": _document_options(catalog),
            "folders": [
                {**row, "document_count": len(catalog.folders.get(str(row["id"]), ()))}
                for row in catalog.folder_tree
            ],
        }
    return {
        "dataset_id": catalog.dataset_id,
        "catalog_source": catalog.source,
        "catalog_version_id": _catalog_version_id(catalog),
        "hierarchical": False,
        "documents": _document_options(catalog),
        "folders": [
            {"id": folder_id, "name": _folder_display_name(folder_id), "document_count": len(doc_ids)}
            for folder_id, doc_ids in catalog.folders.items()
        ],
    }


def _validate_id_array(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"DOCMIND_INVALID_SCOPE: {field} must be an array")
    if not value:
        raise ValueError(f"DOCMIND_INVALID_SCOPE: {field} must contain at least one id")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"DOCMIND_INVALID_SCOPE: {field} must contain non-empty strings")
    normalized = [item.strip() for item in value]
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"DOCMIND_INVALID_SCOPE: {field} must not contain duplicates")
    return normalized


def _resolve_scope(catalog: Catalog, scope: object) -> ResolvedScope:
    if not isinstance(scope, dict):
        raise ValueError("DOCMIND_INVALID_SCOPE: scope must be an object")
    mode = scope.get("mode")
    if mode not in {"all", "folders", "documents"}:
        raise ValueError("DOCMIND_INVALID_SCOPE: mode must be all, folders, or documents")

    expected_keys = {
        "all": {"mode"},
        "folders": {"mode", "folder_ids"},
        "documents": {"mode", "document_ids"},
    }[mode]
    if set(scope) != expected_keys:
        raise ValueError(f"DOCMIND_INVALID_SCOPE: {mode} scope has missing or unknown fields")

    if mode == "all":
        folder_ids = tuple(catalog.folders)
        document_ids = tuple(dict.fromkeys(doc_id for ids in catalog.folders.values() for doc_id in ids))
        return ResolvedScope(mode=mode, folder_ids=folder_ids, document_ids=document_ids)

    if mode == "folders":
        requested = _validate_id_array(scope["folder_ids"], field="folder_ids")
        effective_folders: list[str] = []
        seen_folders: set[str] = set()
        for folder_id in requested:
            for descendant_id in _subtree_ids(catalog, folder_id):
                if descendant_id not in seen_folders:
                    seen_folders.add(descendant_id)
                    effective_folders.append(descendant_id)
        document_ids = tuple(
            dict.fromkeys(
                doc_id
                for folder_id in effective_folders
                for doc_id in catalog.folders.get(folder_id, ())
            )
        )
        return ResolvedScope(mode=mode, folder_ids=tuple(effective_folders), document_ids=document_ids)

    requested_documents = _validate_id_array(scope["document_ids"], field="document_ids")
    document_folders = {
        doc_id: folder_id
        for folder_id, doc_ids in catalog.folders.items()
        for doc_id in doc_ids
    }
    unknown = [doc_id for doc_id in requested_documents if doc_id not in document_folders]
    if unknown:
        raise ValueError("DOCMIND_INVALID_DOCUMENT: document_ids contains an unknown or inaccessible document")
    folder_ids = tuple(dict.fromkeys(document_folders[doc_id] for doc_id in requested_documents))
    return ResolvedScope(mode=mode, folder_ids=folder_ids, document_ids=tuple(requested_documents))


def _bounded_text(value: object) -> str:
    """Return the C-search reranker prefix in Python string units."""

    return str(value or "")[:_RERANK_TEXT_LIMIT]


def _enrich_parser_platform_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
    metadata = chunk.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = None
    parser_platform = metadata.get("parser_platform") if isinstance(metadata, dict) else None
    if not isinstance(parser_platform, dict):
        return chunk
    chunk["parse_run_id"] = chunk.get("parse_run_id") or parser_platform.get("parse_run_id")
    chunk["chunk_set_id"] = chunk.get("chunk_set_id") or parser_platform.get("chunk_set_id")
    chunk["parser_platform"] = parser_platform
    chunk["provenance"] = parser_platform.get("contributing_provenance") or []
    chunk["office_locator"] = parser_platform.get("office_locator")
    return chunk


def _folder_by_document(catalog: Catalog) -> dict[str, str]:
    return {doc_id: folder_id for folder_id, doc_ids in catalog.folders.items() for doc_id in doc_ids}


async def _lane_candidates(
    tenant_id: str,
    question: str,
    catalog: Catalog,
    resolved_scope: ResolvedScope,
    *,
    candidate_mode: str,
) -> list[Candidate]:
    request = {
        "dataset_ids": [catalog.dataset_id],
        "doc_ids": list(resolved_scope.document_ids),
        "question": question,
        "page": 1,
        "size": _LANE_LIMIT,
        "rerank_candidates_count": _LANE_LIMIT,
        "knn_top_k": _DENSE_K,
        "knn_num_candidates": _DENSE_NUM_CANDIDATES,
        "similarity_threshold": _DENSE_SIMILARITY,
        "use_kg": False,
        "keyword": False,
        "cross_languages": [],
    }
    success, result = await dataset_api_service.search_datasets(
        tenant_id,
        request,
        candidate_mode=candidate_mode,
    )
    if not success:
        raise RuntimeError(f"DOCMIND_{candidate_mode.upper()}_RECALL_FAILED: {result}")
    if not isinstance(result, dict) or not isinstance(result.get("chunks"), list):
        raise RuntimeError(f"DOCMIND_{candidate_mode.upper()}_RECALL_MALFORMED")

    allowed_documents = set(resolved_scope.document_ids)
    document_folders = _folder_by_document(catalog)
    seen_chunks: set[str] = set()
    candidates: list[Candidate] = []
    for raw_chunk in result["chunks"][:_LANE_LIMIT]:
        if not isinstance(raw_chunk, dict):
            raise RuntimeError(f"DOCMIND_{candidate_mode.upper()}_RECALL_MALFORMED")
        chunk_id = str(raw_chunk.get("chunk_id") or raw_chunk.get("id") or "")
        doc_id = str(raw_chunk.get("doc_id") or "")
        text = _bounded_text(raw_chunk.get("content_with_weight") or raw_chunk.get("content"))
        if not chunk_id or not doc_id or not text or chunk_id in seen_chunks:
            raise RuntimeError(f"DOCMIND_{candidate_mode.upper()}_RECALL_MALFORMED")
        if doc_id not in allowed_documents:
            raise RuntimeError(f"DOCMIND_{candidate_mode.upper()}_SCOPE_ESCAPE")
        if raw_chunk.get("kb_id") not in (None, "", catalog.dataset_id):
            raise RuntimeError(f"DOCMIND_{candidate_mode.upper()}_SCOPE_ESCAPE")
        seen_chunks.add(chunk_id)
        candidates.append(Candidate(folder_id=document_folders[doc_id], chunk=raw_chunk, rerank_text=text))
    return candidates


def _rrf_candidates(
    bm25_candidates: list[Candidate],
    dense_candidates: list[Candidate],
    *,
    document_limit: int = _DOCUMENT_CANDIDATE_LIMIT,
    candidate_limit: int = _RERANK_CANDIDATE_LIMIT,
) -> list[Candidate]:
    merged: dict[str, dict[str, Any]] = {}
    for lane, lane_candidates in (("bm25", bm25_candidates), ("dense", dense_candidates)):
        for rank, candidate in enumerate(lane_candidates[:_LANE_LIMIT], start=1):
            chunk_id = str(candidate.chunk.get("chunk_id") or candidate.chunk.get("id") or "")
            doc_id = str(candidate.chunk.get("doc_id") or "")
            if not chunk_id or not doc_id:
                raise RuntimeError("DOCMIND_RECALL_MALFORMED")
            state = merged.setdefault(
                chunk_id,
                {"candidate": candidate, "doc_id": doc_id, "rrf_score": 0.0, "bm25_rank": None, "dense_rank": None},
            )
            if state["doc_id"] != doc_id:
                raise RuntimeError("DOCMIND_CHUNK_PROVENANCE_CONFLICT")
            state[f"{lane}_rank"] = rank
            state["rrf_score"] += 1.0 / (_RRF_K + rank)

    ordered = sorted(merged.items(), key=lambda item: (-item[1]["rrf_score"], item[0]))
    per_document: dict[str, int] = {}
    result: list[Candidate] = []
    for _chunk_id, state in ordered:
        doc_id = state["doc_id"]
        if per_document.get(doc_id, 0) >= document_limit:
            continue
        base = state["candidate"]
        per_document[doc_id] = per_document.get(doc_id, 0) + 1
        result.append(
            Candidate(
                folder_id=base.folder_id,
                chunk=base.chunk,
                rerank_text=base.rerank_text,
                rrf_score=state["rrf_score"],
                bm25_rank=state["bm25_rank"],
                dense_rank=state["dense_rank"],
            )
        )
        if len(result) == candidate_limit:
            break
    return result


def _rerank_model(catalog: Catalog) -> LLMBundle:
    exists, knowledgebase = KnowledgebaseService.get_by_id(catalog.dataset_id)
    if not exists:
        raise RuntimeError("DocMind catalog dataset is unavailable")
    rerank_id = os.environ.get("DOCMIND_RERANK_ID", _DEFAULT_RERANK_ID)
    if rerank_id.split("@", 1)[0] != "jina-reranker-v3.5":
        raise RuntimeError("DOCMIND_RERANK_MODEL_INVALID: jina-reranker-v3.5 is required")
    if os.environ.get("DOCMIND_GENERATIONLESS_E2E_ENABLED") == "1":
        api_key = _generationless_jina_api_key()
        if len(api_key) < 12 or any(marker in api_key for marker in ("CHANGE_ME", "PLACEHOLDER", "GENERATED_LOCALLY")):
            raise RuntimeError("DOCMIND_RERANK_CREDENTIAL_UNAVAILABLE")
        # The isolated generationless overlay keeps the credential process-only:
        # do not require or create a tenant provider row just to run C-search.
        model_config = {
            "llm_factory": "Jina",
            "api_key": api_key,
            "llm_name": "jina-reranker-v3.5",
            "api_base": "https://api.jina.ai/v1/rerank",
            "model_type": LLMType.RERANK.value,
            "max_tokens": 8192,
        }
    else:
        model_config = get_model_config_from_provider_instance(knowledgebase.tenant_id, LLMType.RERANK, rerank_id)
    return LLMBundle(knowledgebase.tenant_id, model_config)


def _generationless_jina_api_key() -> str:
    secret_file = os.environ.get("JINA_API_KEY_FILE", "").strip()
    if not secret_file:
        return os.environ.get("JINA_API_KEY", "")

    try:
        path = Path(secret_file)
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise OSError("invalid secret file")
        encoded = path.read_bytes()
        if not encoded or len(encoded) > 16 * 1024:
            raise OSError("invalid secret file size")
        value = encoded.decode("utf-8").strip()
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("invalid secret file content")
        return value
    except (OSError, UnicodeError, ValueError):
        # A configured file is authoritative. Never fall back to an environment
        # value when its mount is missing or malformed.
        return ""


def _empty_result(catalog: Catalog, resolved_scope: ResolvedScope, trace_id: str, started_at: float) -> dict[str, Any]:
    return {
        "dataset_id": catalog.dataset_id,
        "project_id": catalog.dataset_id,
        "catalog_source": catalog.source,
        "catalog_version_id": _catalog_version_id(catalog),
        "scope_mode": resolved_scope.mode,
        "effective_folder_ids": list(resolved_scope.folder_ids),
        "scope_doc_ids": list(resolved_scope.document_ids),
        "selected_folders": [{"id": folder_id} for folder_id in resolved_scope.folder_ids],
        "chunks": [],
        "ranked_chunks": [],
        "ranked_total": 0,
        "total": 0,
        "candidate_count": 0,
        "lane_counts": {"bm25": 0, "dense": 0},
        "trace_id": trace_id,
        "timings_ms": {"recall": 0.0, "rerank": 0.0, "total": round((time.monotonic() - started_at) * 1000, 2)},
        "caps": {"lane": _LANE_LIMIT, "candidates": _RERANK_CANDIDATE_LIMIT, "per_document": _DOCUMENT_CANDIDATE_LIMIT, "results": _FINAL_RESULT_LIMIT},
    }


async def search(
    tenant_id: str,
    question: str,
    *,
    project_id: str | None = None,
    scope: object,
) -> dict[str, Any]:
    catalog = _load_catalog()
    if not KnowledgebaseService.accessible(catalog.dataset_id, tenant_id):
        raise PermissionError("DocMind catalog dataset is not accessible")
    if project_id is not None and (not isinstance(project_id, str) or project_id != catalog.dataset_id):
        raise ValueError("DOCMIND_INVALID_PROJECT: project_id is unknown or inaccessible")
    question = str(question or "").strip()
    if not question:
        raise ValueError("DOCMIND_INVALID_QUESTION: question is required")

    resolved_scope = _resolve_scope(catalog, scope)
    trace_id = str(uuid4())
    started_at = time.monotonic()
    if not resolved_scope.document_ids:
        return _empty_result(catalog, resolved_scope, trace_id, started_at)

    recall_started_at = time.monotonic()
    try:
        bm25_candidates, dense_candidates = await asyncio.wait_for(
            asyncio.gather(
                _lane_candidates(tenant_id, question, catalog, resolved_scope, candidate_mode="bm25"),
                _lane_candidates(tenant_id, question, catalog, resolved_scope, candidate_mode="dense"),
            ),
            timeout=_RECALL_TIMEOUT_SECONDS,
        )
    except TimeoutError as error:
        raise RuntimeError("DOCMIND_RECALL_TIMEOUT") from error
    recalled_at = time.monotonic()
    candidates = _rrf_candidates(bm25_candidates, dense_candidates)
    if not candidates:
        result = _empty_result(catalog, resolved_scope, trace_id, started_at)
        result["lane_counts"] = {"bm25": len(bm25_candidates), "dense": len(dense_candidates)}
        result["timings_ms"]["recall"] = round((recalled_at - recall_started_at) * 1000, 2)
        result["timings_ms"]["total"] = round((recalled_at - started_at) * 1000, 2)
        return result

    rerank_candidates = sorted(
        candidates,
        key=lambda candidate: str(candidate.chunk.get("chunk_id") or candidate.chunk.get("id")),
    )
    reranker = _rerank_model(catalog)
    try:
        scores, used_tokens = await asyncio.wait_for(
            asyncio.to_thread(reranker.similarity, question, [candidate.rerank_text for candidate in rerank_candidates]),
            timeout=_RERANK_TIMEOUT_SECONDS,
        )
    except TimeoutError as error:
        raise RuntimeError("DOCMIND_RERANK_TIMEOUT") from error
    except Exception as error:
        raise RuntimeError("DOCMIND_RERANK_FAILED") from error
    try:
        raw_scores = list(scores)
        score_values = [float(score) for score in raw_scores]
    except (TypeError, ValueError):
        raise RuntimeError("DOCMIND_RERANK_RESPONSE_INVALID")
    if len(score_values) != len(rerank_candidates) or any(
        isinstance(score, bool) or not math.isfinite(value)
        for score, value in zip(raw_scores, score_values, strict=True)
    ):
        raise RuntimeError("DOCMIND_RERANK_RESPONSE_INVALID")

    scored = list(zip(rerank_candidates, score_values, strict=True))
    scored.sort(
        key=lambda item: (
            -item[1],
            -item[0].rrf_score,
            str(item[0].chunk.get("chunk_id") or item[0].chunk.get("id")),
        )
    )
    ranked_chunks: list[dict[str, Any]] = []
    for candidate, score in scored[:_FINAL_RESULT_LIMIT]:
        chunk = _enrich_parser_platform_chunk(dict(candidate.chunk))
        chunk["similarity"] = score
        chunk["rerank_score"] = score
        chunk["rrf_score"] = candidate.rrf_score
        chunk["bm25_rank"] = candidate.bm25_rank
        chunk["dense_rank"] = candidate.dense_rank
        chunk["folder_id"] = candidate.folder_id
        ranked_chunks.append(chunk)

    relative_paths = await asyncio.to_thread(
        document_relative_paths,
        catalog.dataset_id,
        {chunk["doc_id"] for chunk in ranked_chunks},
    )
    for chunk in ranked_chunks:
        chunk.pop("document_relative_path", None)
        if relative_path := relative_paths.get(chunk["doc_id"]):
            chunk["document_relative_path"] = relative_path

    finished_at = time.monotonic()
    timings_ms = {
        "recall": round((recalled_at - recall_started_at) * 1000, 2),
        "rerank": round((finished_at - recalled_at) * 1000, 2),
        "total": round((finished_at - started_at) * 1000, 2),
    }
    logger.info(
        "DocMind C-search trace=%s scope=%s docs=%d bm25=%d dense=%d candidates=%d results=%d timings_ms=%s",
        trace_id,
        resolved_scope.mode,
        len(resolved_scope.document_ids),
        len(bm25_candidates),
        len(dense_candidates),
        len(candidates),
        len(ranked_chunks),
        timings_ms,
    )
    return {
        "dataset_id": catalog.dataset_id,
        "project_id": catalog.dataset_id,
        "catalog_source": catalog.source,
        "catalog_version_id": _catalog_version_id(catalog),
        "scope_mode": resolved_scope.mode,
        "effective_folder_ids": list(resolved_scope.folder_ids),
        "scope_doc_ids": list(resolved_scope.document_ids),
        "selected_folders": [{"id": folder_id} for folder_id in resolved_scope.folder_ids],
        "chunks": ranked_chunks,
        "ranked_chunks": ranked_chunks,
        "ranked_total": len(ranked_chunks),
        "total": len(ranked_chunks),
        "candidate_count": len(candidates),
        "lane_counts": {"bm25": len(bm25_candidates), "dense": len(dense_candidates)},
        "trace_id": trace_id,
        "timings_ms": timings_ms,
        "rerank_used_tokens": used_tokens,
        "caps": {"lane": _LANE_LIMIT, "candidates": _RERANK_CANDIDATE_LIMIT, "per_document": _DOCUMENT_CANDIDATE_LIMIT, "results": _FINAL_RESULT_LIMIT},
    }
