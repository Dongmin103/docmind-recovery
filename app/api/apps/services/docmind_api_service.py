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

import requests

from api.apps.services import dataset_api_service, docmind_catalog_shadow_service, docmind_llm_router_service
from api.db.joint_services.tenant_model_service import get_model_config_from_provider_instance
from api.db.services import docmind_catalog_service
from api.db.services.docmind_document_path_service import document_relative_paths
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from common.constants import LLMType

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).with_name("docmind_catalog.json")
_MIN_FOLDER_COUNT = 1
_MAX_FOLDER_COUNT = 5
_MAX_DOCUMENT_BEARING_FOLDER_COUNT = 8
_ROUTING_WINDOW_LIMIT = 10
_CUMULATIVE_PROBABILITY_THRESHOLD = 0.85
_ROUTING_SOFTMAX_TEMPERATURE = 0.15
_ROUTING_AMBIGUOUS_MARGIN = 0.10
_ROUTING_VERY_AMBIGUOUS_MARGIN = 0.02
_ROUTING_LOW_SCORE_FLOOR = 0.1844201862812042
_RECALL_LIMIT = 500
_DOCUMENT_CANDIDATE_LIMIT = 8
_FOLDER_CANDIDATE_FLOOR = 8
_FOLDER_CANDIDATE_LIMIT = 32
_FINAL_RESULT_LIMIT = 5
_RERANK_CANDIDATE_LIMIT = 64
_DEFAULT_RERANK_ID = "jina-reranker-v3@jina@Jina"
_RECALL_TIMEOUT_SECONDS = 60
_RERANK_TIMEOUT_SECONDS = 45
_FOLDER_SEARCH_SEMAPHORE = asyncio.Semaphore(3)
_HEX32_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ROOT_URI_PATTERN = re.compile(r"^viking://resources/[a-z0-9]+(?:-[a-z0-9]+)*/$")


@dataclass(frozen=True)
class Catalog:
    dataset_id: str
    root_uri: str
    folders: dict[str, tuple[str, ...]]
    source: str = "static"
    version_id: str | None = None
    folder_tree: tuple[dict[str, Any], ...] = ()


class DocmindCatalogNotInitializedError(RuntimeError):
    """Raised when a database-primary workspace has no published Catalog yet."""

    code = "DOCMIND_CATALOG_NOT_INITIALIZED"


@dataclass(frozen=True)
class Candidate:
    folder_id: str
    chunk: dict[str, Any]
    rerank_text: str


@dataclass(frozen=True)
class FolderCandidatePool:
    candidates: list[Candidate]
    hybrid_candidate_count: int
    dense_rescue_triggered: bool
    dense_candidate_count: int
    dense_recall_ms: float


@dataclass(frozen=True)
class LayoutCandidateNormalization:
    """Normalized result for sanitized OpenViking sidecar records."""

    candidates: list[dict[str, Any]]
    trace: dict[str, Any]


@dataclass(frozen=True)
class FolderSelectionConfig:
    """Immutable, evaluator-supplied folder-count policy.

    ``None`` adaptive thresholds preserve the deployed cumulative-only policy.
    The config is deliberately internal: request payloads cannot select routing
    policy or disclose routing telemetry.
    """

    cumulative_threshold: float = _CUMULATIVE_PROBABILITY_THRESHOLD
    temperature: float = _ROUTING_SOFTMAX_TEMPERATURE
    ambiguous_margin: float | None = None
    very_ambiguous_margin: float | None = None
    low_score_floor: float | None = None


@dataclass(frozen=True)
class FolderSelectionDecision:
    """Selection result plus evaluator-safe, URI-free decision metadata."""

    folders: list[dict[str, Any]]
    metadata: dict[str, Any]


_LAYOUT_SIDECAR_CATEGORIES = frozenset({"canonical_sidecar", "nested_manifest_sidecar"})


def _normalize_layout_candidates(layout_records: list[dict[str, Any]]) -> LayoutCandidateNormalization:
    """Collapse sanitized evaluator layout records to one finite candidate per folder.

    The URI-free record contract is ``folder_id``, ``sidecar_category``,
    ``score``, ``tie_breaker``, and optional trace-only ``level``. Higher score
    wins duplicate folders; equal scores prefer canonical sidecars over nested
    manifest sidecars, then use lexical ``tie_breaker`` order. Candidate output
    is ordered by score descending and folder ID, while ``level`` is retained
    only in the evaluator trace and never influences eligibility or ties.
    """

    resource_counts = {"canonical_sidecar": 0, "nested_manifest_sidecar": 0, "rejected": 0}
    winners: dict[str, tuple[float, str, object, str]] = {}
    for record in layout_records:
        if not isinstance(record, dict):
            resource_counts["rejected"] += 1
            continue

        folder_id = record.get("folder_id")
        sidecar_category = record.get("sidecar_category")
        tie_breaker = record.get("tie_breaker")
        if (
            not isinstance(folder_id, str)
            or not folder_id
            or sidecar_category not in _LAYOUT_SIDECAR_CATEGORIES
            or not isinstance(tie_breaker, str)
            or not tie_breaker
            or "://" in tie_breaker
            or "uri" in record
        ):
            resource_counts["rejected"] += 1
            continue
        try:
            score = float(record.get("score"))
        except (TypeError, ValueError):
            resource_counts["rejected"] += 1
            continue
        if isinstance(record.get("score"), bool) or not math.isfinite(score):
            resource_counts["rejected"] += 1
            continue

        resource_counts[sidecar_category] += 1
        level = record.get("level")
        current = winners.get(folder_id)
        if current is None:
            winners[folder_id] = (score, sidecar_category, level, tie_breaker)
            continue

        current_score, current_category, _current_level, current_tie_breaker = current
        current_rank = 0 if current_category == "canonical_sidecar" else 1
        candidate_rank = 0 if sidecar_category == "canonical_sidecar" else 1
        if (-score, candidate_rank, tie_breaker) < (-current_score, current_rank, current_tie_breaker):
            winners[folder_id] = (score, sidecar_category, level, tie_breaker)

    ordered_winners = sorted(winners.items(), key=lambda item: (-item[1][0], item[0]))
    candidates = [{"id": folder_id, "score": score} for folder_id, (score, _category, _level, _tie_breaker) in ordered_winners]
    trace = {
        "resource_counts": resource_counts,
        "folders": [
            {
                "id": folder_id,
                "winning_sidecar": category,
                "raw_score": score,
                "level": level,
            }
            for folder_id, (score, category, level, _tie_breaker) in ordered_winners
        ],
    }
    return LayoutCandidateNormalization(candidates=candidates, trace=trace)


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
    if not _ROOT_URI_PATTERN.fullmatch(root_uri):
        raise ValueError("DocMind catalog root_uri is invalid")
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
        if not _SLUG_PATTERN.fullmatch(folder_id) or folder_id in folders or not doc_ids or len(set(doc_ids)) != len(doc_ids):
            raise ValueError("DocMind catalog contains an invalid or duplicate folder")
        if any(not _HEX32_PATTERN.fullmatch(doc_id) or doc_id in seen_documents for doc_id in doc_ids):
            raise ValueError("DocMind catalog contains an invalid or cross-folder duplicate document")
        seen_documents.update(doc_ids)
        folders[folder_id] = doc_ids
    return Catalog(
        dataset_id=dataset_id,
        root_uri=root_uri,
        folders=folders,
        source="static",
        version_id=None,
    )


def _load_catalog() -> Catalog:
    if os.environ.get("DOCMIND_EMERGENCY_STATIC_FALLBACK") == "1":
        return _load_static_catalog()
    if os.environ.get("DOCMIND_CATALOG_DB_PRIMARY_ENABLED") == "1":
        loaded = docmind_catalog_service.load_default_database_serving_catalog()
        if loaded is None:
            try:
                static_catalog = _load_static_catalog()
            except FileNotFoundError as error:
                raise DocmindCatalogNotInitializedError(
                    DocmindCatalogNotInitializedError.code
                ) from error
            loaded = docmind_catalog_service.load_database_serving_catalog(
                static_catalog.dataset_id
            )
            if loaded is None:
                return static_catalog
    else:
        return _load_static_catalog()
    return Catalog(
        dataset_id=loaded.dataset_id,
        root_uri=loaded.root_uri,
        folders={key: tuple(value) for key, value in loaded.folders.items()},
        source="database",
        version_id=loaded.active_version_id,
        folder_tree=tuple(
            dict(row) for row in getattr(loaded, "folder_tree", ())
        ),
    )


def _catalog_version_id(catalog: Catalog) -> str:
    if catalog.version_id:
        return catalog.version_id
    snapshot = {
        "dataset_id": catalog.dataset_id,
        "root_uri": catalog.root_uri,
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


def _is_hierarchical(catalog: Catalog) -> bool:
    return bool(catalog.folder_tree)


def _tree_by_id(catalog: Catalog) -> dict[str, dict[str, Any]]:
    return {str(row["id"]): row for row in catalog.folder_tree}


def _subtree_ids(catalog: Catalog, folder_id: str) -> list[str]:
    if not _is_hierarchical(catalog):
        return [folder_id]
    rows = _tree_by_id(catalog)
    if folder_id not in rows:
        raise ValueError("DOCMIND_INVALID_FOLDER: folder_ids contains an unknown folder")
    children: dict[str | None, list[str]] = {}
    for row in catalog.folder_tree:
        children.setdefault(row.get("parent_id"), []).append(str(row["id"]))
    for values in children.values():
        values.sort(
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


def _expand_document_folders(
    catalog: Catalog,
    routed_folders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not _is_hierarchical(catalog):
        return routed_folders
    expanded: list[dict[str, Any]] = []
    seen: set[str] = set()
    for routed in routed_folders:
        eligible = [
            node_id
            for node_id in _subtree_ids(catalog, str(routed["id"]))
            if catalog.folders.get(node_id)
        ]
        if not eligible:
            continue
        probability = float(routed.get("probability") or 0.0) / len(eligible)
        for node_id in eligible:
            if node_id in seen:
                continue
            seen.add(node_id)
            expanded.append(
                {
                    **routed,
                    "id": node_id,
                    "score": probability,
                    "raw_score": float(routed.get("raw_score") or 0.0),
                    "probability": probability,
                    "routed_from_id": routed["id"],
                    "selection_reason": (
                        routed.get("selection_reason")
                        if node_id == routed["id"]
                        else "subtree_expansion"
                    ),
                }
            )
            if len(expanded) == _MAX_DOCUMENT_BEARING_FOLDER_COUNT:
                break
        if len(expanded) == _MAX_DOCUMENT_BEARING_FOLDER_COUNT:
            break
    cumulative = 0.0
    probability_total = sum(float(row.get("probability") or 0.0) for row in expanded)
    for row in expanded:
        normalized = (
            float(row.get("probability") or 0.0) / probability_total
            if probability_total > 0
            else 1.0 / len(expanded)
        )
        row["probability"] = normalized
        row["score"] = normalized
        cumulative += normalized
        row["cumulative_probability"] = cumulative
    return expanded


def _search_caps(catalog: Catalog | None = None) -> dict[str, int]:
    return {
        "folders": (
            _MAX_DOCUMENT_BEARING_FOLDER_COUNT
            if catalog is not None and _is_hierarchical(catalog)
            else _MAX_FOLDER_COUNT
        ),
        "candidates": _RERANK_CANDIDATE_LIMIT,
        "per_folder": _FOLDER_CANDIDATE_LIMIT,
        "per_document": _DOCUMENT_CANDIDATE_LIMIT,
        "results": _FINAL_RESULT_LIMIT,
    }


def list_folders(tenant_id: str) -> dict[str, Any]:
    catalog = _load_catalog()
    if not KnowledgebaseService.accessible(catalog.dataset_id, tenant_id):
        raise PermissionError("DocMind catalog dataset is not accessible")
    if not _is_hierarchical(catalog) and len(catalog.folders) != _MAX_FOLDER_COUNT:
        raise RuntimeError("DocMind serving catalog must contain exactly five folders")
    if _is_hierarchical(catalog):
        tree = [
            {
                **row,
                "document_count": len(catalog.folders.get(str(row["id"]), ())),
            }
            for row in catalog.folder_tree
        ]
        return {
            "dataset_id": catalog.dataset_id,
            "catalog_source": catalog.source,
            "catalog_version_id": _catalog_version_id(catalog),
            "hierarchical": True,
            "folders": tree,
        }
    return {
        "dataset_id": catalog.dataset_id,
        "catalog_source": catalog.source,
        "catalog_version_id": _catalog_version_id(catalog),
        "folders": [
            {
                "id": folder_id,
                "name": _folder_display_name(folder_id),
                "document_count": len(doc_ids),
            }
            for folder_id, doc_ids in catalog.folders.items()
        ],
    }


def _select_manual_folders(catalog: Catalog, folder_ids: object) -> list[dict[str, Any]]:
    if not isinstance(folder_ids, list):
        raise ValueError("DOCMIND_INVALID_FOLDER_SCOPE: folder_ids must be an array")
    if not folder_ids:
        raise ValueError("DOCMIND_INVALID_FOLDER_SCOPE: folder_ids must contain at least one folder")
    if len(folder_ids) > _MAX_FOLDER_COUNT:
        raise ValueError(f"DOCMIND_INVALID_FOLDER_SCOPE: folder_ids cannot contain more than {_MAX_FOLDER_COUNT} folders")
    if any(not isinstance(folder_id, str) or not folder_id for folder_id in folder_ids):
        raise ValueError("DOCMIND_INVALID_FOLDER_SCOPE: folder_ids must contain non-empty strings")
    if len(set(folder_ids)) != len(folder_ids):
        raise ValueError("DOCMIND_INVALID_FOLDER_SCOPE: folder_ids must not contain duplicates")

    requested = set(folder_ids)
    valid_folder_ids = (
        set(_tree_by_id(catalog)) if _is_hierarchical(catalog) else set(catalog.folders)
    )
    if requested.difference(valid_folder_ids):
        raise ValueError("DOCMIND_INVALID_FOLDER: folder_ids contains an unknown folder")

    canonical_source = (
        [str(row["id"]) for row in catalog.folder_tree]
        if _is_hierarchical(catalog)
        else list(catalog.folders)
    )
    canonical_ids = [folder_id for folder_id in canonical_source if folder_id in requested]
    probability = 1.0 / len(canonical_ids)
    return [
        {
            "id": folder_id,
            "score": probability,
            "raw_score": probability,
            "probability": probability,
            "cumulative_probability": probability * (index + 1),
            "selection_reason": "manual_scope",
        }
        for index, folder_id in enumerate(canonical_ids)
    ]


def _bounded_text(value: object) -> str:
    text = str(value or "")
    if len(text) <= 1800:
        return text
    boundary = max(text.rfind(mark, 0, 1800) for mark in ("\n", ".", "?", "!"))
    return text[: boundary if boundary >= 800 else 1800]


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


def _select_dynamic_folders(
    candidates: list[dict[str, Any]],
    *,
    min_folders: int = _MIN_FOLDER_COUNT,
    max_folders: int = _MAX_FOLDER_COUNT,
    cumulative_threshold: float = _CUMULATIVE_PROBABILITY_THRESHOLD,
    temperature: float = _ROUTING_SOFTMAX_TEMPERATURE,
    selection_config: FolderSelectionConfig | None = None,
) -> list[dict[str, Any]]:
    """Select folders using the current policy or an evaluator-only policy.

    Keeping this list-returning wrapper protects the deployed caller contract.
    Evaluators that need decision diagnostics use
    :func:`_select_dynamic_folder_decision` directly.
    """
    if selection_config is None:
        selection_config = FolderSelectionConfig(
            cumulative_threshold=cumulative_threshold,
            temperature=temperature,
        )
    elif cumulative_threshold != _CUMULATIVE_PROBABILITY_THRESHOLD or temperature != _ROUTING_SOFTMAX_TEMPERATURE:
        raise ValueError("selection_config cannot be combined with legacy scoring arguments")
    return _select_dynamic_folder_decision(
        candidates,
        min_folders=min_folders,
        max_folders=max_folders,
        selection_config=selection_config,
    ).folders


def _select_dynamic_folder_decision(
    candidates: list[dict[str, Any]],
    *,
    min_folders: int = _MIN_FOLDER_COUNT,
    max_folders: int = _MAX_FOLDER_COUNT,
    selection_config: FolderSelectionConfig = FolderSelectionConfig(),
) -> FolderSelectionDecision:
    if min_folders < 1 or max_folders < min_folders:
        raise ValueError("invalid dynamic folder bounds")
    if not isinstance(selection_config, FolderSelectionConfig):
        raise ValueError("invalid adaptive folder selection config")
    cumulative_threshold = selection_config.cumulative_threshold
    temperature = selection_config.temperature
    if (
        isinstance(cumulative_threshold, bool)
        or isinstance(temperature, bool)
        or not isinstance(cumulative_threshold, (int, float))
        or not isinstance(temperature, (int, float))
        or not 0 < cumulative_threshold <= 1
        or not math.isfinite(cumulative_threshold)
        or temperature <= 0
        or not math.isfinite(temperature)
    ):
        raise ValueError("invalid dynamic folder scoring configuration")
    ambiguous_margin = selection_config.ambiguous_margin
    very_ambiguous_margin = selection_config.very_ambiguous_margin
    low_score_floor = selection_config.low_score_floor
    adaptive_values = (ambiguous_margin, very_ambiguous_margin, low_score_floor)
    if any(
        value is not None
        and (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value))
        for value in adaptive_values
    ):
        raise ValueError("invalid adaptive folder scoring configuration")
    if (ambiguous_margin is None) != (very_ambiguous_margin is None):
        raise ValueError("adaptive margin thresholds must be configured together")
    if ambiguous_margin is not None and not 0 <= very_ambiguous_margin < ambiguous_margin <= 1:
        raise ValueError("invalid adaptive folder margin thresholds")

    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            raw_score = float(candidate.get("score"))
        except (TypeError, ValueError):
            continue
        folder_id = str(candidate.get("id") or "")
        if not folder_id or not math.isfinite(raw_score):
            continue
        scored.append({"id": folder_id, "score": raw_score, "raw_score": raw_score})

    scored.sort(key=lambda item: (-item["raw_score"], item["id"]))
    if len(scored) < min_folders:
        raise RuntimeError("DocMind folder routing returned fewer than the required catalog folders")

    max_score = scored[0]["raw_score"]
    weights = [math.exp((item["raw_score"] - max_score) / temperature) for item in scored]
    weight_total = sum(weights)
    if not math.isfinite(weight_total) or weight_total <= 0:
        raise RuntimeError("DocMind folder routing scores could not be normalized")

    probabilities = [weight / weight_total for weight in weights]
    cumulative_probabilities: list[float] = []
    cumulative = 0.0
    for probability in probabilities:
        cumulative += probability
        cumulative_probabilities.append(cumulative)

    effective_max_folders = min(max_folders, len(scored))
    k_cumulative = min_folders
    while k_cumulative < effective_max_folders and cumulative_probabilities[k_cumulative - 1] < cumulative_threshold:
        k_cumulative += 1
    s1 = scored[0]["raw_score"]
    p1 = probabilities[0]
    p2 = probabilities[1] if len(probabilities) > 1 else 0.0
    margin12 = p1 - p2
    k_margin = min_folders
    k_absolute = min_folders
    adaptive_enabled = ambiguous_margin is not None or low_score_floor is not None
    if ambiguous_margin is not None:
        if margin12 < very_ambiguous_margin:
            k_margin = min(effective_max_folders, 4)
        elif margin12 < ambiguous_margin:
            k_margin = min(effective_max_folders, 3)
    if low_score_floor is not None and s1 < low_score_floor:
        k_absolute = effective_max_folders
    selected_count = max(k_cumulative, k_margin, k_absolute)

    if k_absolute > max(k_cumulative, k_margin):
        decision_reason = "low_score_expansion"
    elif k_margin > k_cumulative:
        decision_reason = "margin_expansion"
    elif k_cumulative == min_folders:
        decision_reason = "minimum"
    elif k_cumulative == effective_max_folders and cumulative_probabilities[effective_max_folders - 1] < cumulative_threshold:
        decision_reason = "maximum"
    else:
        decision_reason = "cumulative_threshold"

    selected: list[dict[str, Any]] = []
    for index, candidate in enumerate(scored[:selected_count]):
        if not adaptive_enabled:
            folder_reason = "minimum" if index < min_folders else "cumulative_threshold"
            if (
                index == selected_count - 1
                and selected_count == effective_max_folders
                and cumulative_probabilities[index] < cumulative_threshold
            ):
                folder_reason = "maximum"
        else:
            folder_reason = "minimum" if index < min_folders else decision_reason
        selected.append(
            {
                **candidate,
                "probability": probabilities[index],
                "cumulative_probability": cumulative_probabilities[index],
                "selection_reason": folder_reason,
            }
        )

    # Preserve the exact deployed representation unless an evaluator explicitly
    # enabled an adaptive threshold. Metadata remains detached from REST output.
    metadata = {
        "selected_count": selected_count,
        "s1": s1,
        "p1": p1,
        "p2": p2,
        "margin12": margin12,
        "cumulative_probability": cumulative_probabilities[selected_count - 1],
        "temperature": temperature,
        "cumulative_threshold": cumulative_threshold,
        "ambiguous_margin": ambiguous_margin,
        "very_ambiguous_margin": very_ambiguous_margin,
        "low_score_floor": low_score_floor,
        "selection_reason": decision_reason,
        "adaptive_enabled": adaptive_enabled,
    }
    return FolderSelectionDecision(folders=selected, metadata=metadata)


def _allocate_folder_quotas(
    selected_folders: list[dict[str, Any]],
    folder_document_counts: dict[str, int],
    *,
    total_budget: int = _RERANK_CANDIDATE_LIMIT,
    floor: int = _FOLDER_CANDIDATE_FLOOR,
    folder_cap: int = _FOLDER_CANDIDATE_LIMIT,
    document_cap: int = _DOCUMENT_CANDIDATE_LIMIT,
    folder_candidate_capacities: dict[str, int] | None = None,
) -> dict[str, int]:
    if not selected_folders or total_budget < 1 or floor < 0 or folder_cap < 1 or document_cap < 1:
        raise ValueError("invalid folder quota configuration")

    capacities: dict[str, int] = {}
    probabilities: dict[str, float] = {}
    for folder in selected_folders:
        folder_id = str(folder["id"])
        capacity = min(folder_cap, max(0, int(folder_document_counts.get(folder_id, 0))) * document_cap)
        if folder_candidate_capacities is not None:
            capacity = min(capacity, max(0, int(folder_candidate_capacities.get(folder_id, 0))))
        capacities[folder_id] = capacity
        probabilities[folder_id] = max(0.0, float(folder.get("probability") or 0.0))

    quotas = {folder_id: min(floor, capacity) for folder_id, capacity in capacities.items()}
    allocated = sum(quotas.values())
    if allocated > total_budget:
        raise ValueError("folder quota floors exceed total budget")

    remaining = total_budget - allocated
    while remaining > 0:
        active = [folder_id for folder_id, capacity in capacities.items() if quotas[folder_id] < capacity]
        if not active:
            break
        probability_total = sum(probabilities[folder_id] for folder_id in active)
        if probability_total <= 0:
            weights = {folder_id: 1 / len(active) for folder_id in active}
        else:
            weights = {folder_id: probabilities[folder_id] / probability_total for folder_id in active}

        exact = {folder_id: remaining * weights[folder_id] for folder_id in active}
        granted = 0
        for folder_id in active:
            grant = min(capacities[folder_id] - quotas[folder_id], int(exact[folder_id]))
            quotas[folder_id] += grant
            granted += grant
        remaining -= granted
        if remaining <= 0:
            break

        ranked = sorted(
            (folder_id for folder_id in active if quotas[folder_id] < capacities[folder_id]),
            key=lambda folder_id: (-(exact[folder_id] - int(exact[folder_id])), -weights[folder_id], folder_id),
        )
        if not ranked:
            break
        for folder_id in ranked:
            if remaining <= 0:
                break
            quotas[folder_id] += 1
            remaining -= 1

    return quotas


def _openviking_settings() -> tuple[str, str]:
    endpoint = os.environ.get("DOCMIND_OPENVIKING_URL", "").rstrip("/")
    api_key = os.environ.get("DOCMIND_OPENVIKING_API_KEY", "")
    api_key_file = os.environ.get("DOCMIND_OPENVIKING_API_KEY_FILE", "")
    if api_key_file:
        api_key = Path(api_key_file).read_text(encoding="utf-8").strip()
    if not endpoint or not api_key:
        raise RuntimeError("DocMind folder routing is not configured")
    return endpoint, api_key


def _find_folders(question: str, catalog: Catalog, trace_id: str) -> list[dict[str, Any]]:
    endpoint, api_key = _openviking_settings()
    response = requests.post(
        f"{endpoint}/api/v1/search/find",
        headers={"Content-Type": "application/json", "X-API-Key": api_key, "X-Request-ID": trace_id},
        json={
            "query": question,
            "target_uri": catalog.root_uri,
            "node_limit": min(_ROUTING_WINDOW_LIMIT, len(catalog.folders)),
            "level": "0,1",
        },
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("status") != "ok":
        raise RuntimeError("DocMind folder routing did not return success")

    layout_records: list[dict[str, Any]] = []
    result = body.get("result") if isinstance(body, dict) else None
    resources = result.get("resources") if isinstance(result, dict) else None
    if not isinstance(resources, list):
        raise RuntimeError("DocMind folder routing did not return a resource list")
    if not resources:
        if _is_hierarchical(catalog):
            return []
        folder_ids = sorted(catalog.folders)
        probability = 1 / len(folder_ids)
        return [
            {
                "id": folder_id,
                "score": 0.0,
                "raw_score": 0.0,
                "probability": probability,
                "cumulative_probability": probability * (index + 1),
                "selection_reason": "no_catalog_fallback",
            }
            for index, folder_id in enumerate(folder_ids)
        ]
    sidecar_categories = {
        ".abstract.md": "canonical_sidecar",
        ".overview.md": "canonical_sidecar",
        "manifest.md/.abstract.md": "nested_manifest_sidecar",
        "manifest.md/.overview.md": "nested_manifest_sidecar",
    }
    hierarchy_paths = (
        {
            str(row.get("relative_path") or "").strip("/"): str(row["id"])
            for row in catalog.folder_tree
        }
        if _is_hierarchical(catalog)
        else {}
    )
    ordered_sidecars = sorted(sidecar_categories, key=len, reverse=True)
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        uri = resource.get("uri")
        if not isinstance(uri, str) or not uri.startswith(catalog.root_uri):
            continue
        remainder = uri.removeprefix(catalog.root_uri).strip("/")
        if _is_hierarchical(catalog):
            sidecar_path = next(
                (
                    suffix
                    for suffix in ordered_sidecars
                    if remainder.endswith(f"/{suffix}")
                ),
                None,
            )
            folder_path = (
                remainder[: -(len(sidecar_path) + 1)] if sidecar_path else ""
            )
            folder_id = hierarchy_paths.get(folder_path, "")
            separator = bool(folder_id and sidecar_path)
        else:
            folder_id, raw_separator, sidecar_path = remainder.partition("/")
            separator = bool(raw_separator)
        sidecar_category = sidecar_categories.get(sidecar_path) if separator else None
        if folder_id not in catalog.folders or sidecar_category is None:
            continue
        layout_records.append(
            {
                "folder_id": folder_id,
                "sidecar_category": sidecar_category,
                "score": resource.get("score"),
                "tie_breaker": remainder,
                "level": resource.get("level"),
            }
        )

    candidates = _normalize_layout_candidates(layout_records).candidates[:_ROUTING_WINDOW_LIMIT]
    if _is_hierarchical(catalog):
        candidates = [row for row in candidates if any(catalog.folders.get(node_id) for node_id in _subtree_ids(catalog, row["id"]))]
        if not candidates:
            return []
    if not candidates:
        raise RuntimeError("DocMind folder routing returned no catalog folder")
    return _select_dynamic_folders(
        candidates,
        max_folders=min(_MAX_FOLDER_COUNT, len(catalog.folders)),
        selection_config=FolderSelectionConfig(
            cumulative_threshold=_CUMULATIVE_PROBABILITY_THRESHOLD,
            temperature=_ROUTING_SOFTMAX_TEMPERATURE,
            ambiguous_margin=_ROUTING_AMBIGUOUS_MARGIN,
            very_ambiguous_margin=_ROUTING_VERY_AMBIGUOUS_MARGIN,
            low_score_floor=_ROUTING_LOW_SCORE_FLOOR,
        ),
    )


async def _route_folders(tenant_id: str, question: str, catalog: Catalog, trace_id: str):
    mode = os.environ.get("DOCMIND_ROUTER_MODE", "llm").strip().lower()
    if mode not in {"llm", "vector"}:
        raise ValueError("DOCMIND_ROUTER_MODE_INVALID")
    fallback_reason = None
    if mode == "llm":
        try:
            folders, metadata = await docmind_llm_router_service.select_folders(tenant_id, question, catalog)
            logger.info("DocMind folder routing trace=%s method=llm cache=%s selected=%d", trace_id, metadata["cache"], len(folders))
            return folders, metadata
        except Exception as error:  # noqa: BLE001 -- provider/DB failures use the existing vector route
            fallback_reason = (
                str(error) if isinstance(error, docmind_llm_router_service.RouterError)
                else "DOCMIND_ROUTER_TIMEOUT" if isinstance(error, TimeoutError)
                else "DOCMIND_ROUTER_MODEL_UNAVAILABLE"
            )
            logger.warning("DocMind folder routing trace=%s fallback=vector reason=%s", trace_id, fallback_reason)
    folders = await asyncio.to_thread(_find_folders, question, catalog, trace_id)
    return folders, {"method": "vector", "cache": "bypassed", "fallback_reason": fallback_reason}


async def _folder_lane_candidates(
    tenant_id: str,
    question: str,
    catalog: Catalog,
    folder_id: str,
    quota: int,
    *,
    candidate_mode: str,
) -> list[Candidate]:
    async with _FOLDER_SEARCH_SEMAPHORE:
        success, result = await dataset_api_service.search_datasets(
            tenant_id,
            {
                "dataset_ids": [catalog.dataset_id],
                "doc_ids": list(catalog.folders[folder_id]),
                "question": question,
                "page": 1,
                "size": _RERANK_CANDIDATE_LIMIT,
                "top_k": _RECALL_LIMIT,
                "rerank_candidates_count": _RERANK_CANDIDATE_LIMIT,
                "use_kg": False,
                "keyword": False,
                "cross_languages": [],
            },
            candidate_mode=candidate_mode,
    )
    if not success:
        raise RuntimeError(f"DocMind {candidate_mode} candidate retrieval failed for {folder_id}: {result}")

    if not isinstance(result, dict):
        raise TypeError(f"DocMind {candidate_mode} candidate retrieval returned malformed data")
    raw_chunks = result.get("chunks")
    if not isinstance(raw_chunks, list):
        raise TypeError(f"DocMind {candidate_mode} candidate retrieval returned malformed chunks")
    per_document: dict[str, int] = {}
    candidates: list[Candidate] = []
    for raw_chunk in raw_chunks:
        if not isinstance(raw_chunk, dict):
            if candidate_mode == "dense":
                raise RuntimeError("DocMind dense candidate is malformed")
            continue
        chunk_id = str(raw_chunk.get("chunk_id") or raw_chunk.get("id") or "")
        doc_id = str(raw_chunk.get("doc_id") or "")
        if doc_id and doc_id not in catalog.folders[folder_id]:
            raise RuntimeError(f"DocMind {candidate_mode} candidate escaped the selected catalog scope")
        if raw_chunk.get("kb_id") not in (None, "", catalog.dataset_id):
            raise RuntimeError(f"DocMind {candidate_mode} candidate escaped the catalog dataset")
        rerank_text = _bounded_text(raw_chunk.get("content_with_weight") or raw_chunk.get("content"))
        if candidate_mode == "dense" and (not chunk_id or not doc_id or not rerank_text):
            raise RuntimeError("DocMind dense candidate is missing required provenance or text")
        if not chunk_id or not doc_id or per_document.get(doc_id, 0) >= _DOCUMENT_CANDIDATE_LIMIT:
            continue
        if not rerank_text:
            continue
        per_document[doc_id] = per_document.get(doc_id, 0) + 1
        candidates.append(Candidate(folder_id=folder_id, chunk=raw_chunk, rerank_text=rerank_text))
        if len(candidates) == quota:
            break
    if candidate_mode == "dense" and not candidates:
        raise RuntimeError("DocMind dense candidate retrieval returned no usable candidates")
    return candidates


def _merge_candidate_lanes(
    hybrid_candidates: list[Candidate],
    dense_candidates: list[Candidate],
    *,
    folder_limit: int = _FOLDER_CANDIDATE_LIMIT,
    document_limit: int = _DOCUMENT_CANDIDATE_LIMIT,
) -> list[Candidate]:
    seen_chunks: set[str] = set()
    per_document: dict[str, int] = {}
    merged: list[Candidate] = []
    for candidate in [*hybrid_candidates, *dense_candidates]:
        chunk_id = str(candidate.chunk.get("chunk_id") or candidate.chunk.get("id") or "")
        doc_id = str(candidate.chunk.get("doc_id") or "")
        if not chunk_id or not doc_id:
            raise RuntimeError("DocMind candidate is missing chunk or document provenance")
        if chunk_id in seen_chunks or per_document.get(doc_id, 0) >= document_limit:
            continue
        seen_chunks.add(chunk_id)
        per_document[doc_id] = per_document.get(doc_id, 0) + 1
        merged.append(candidate)
        if len(merged) == folder_limit:
            break
    return merged


async def _folder_candidates(
    tenant_id: str,
    question: str,
    catalog: Catalog,
    folder_id: str,
    quota: int,
    *,
    dense_rescue_enabled: bool,
) -> FolderCandidatePool:
    hybrid_candidates = await _folder_lane_candidates(
        tenant_id,
        question,
        catalog,
        folder_id,
        quota,
        candidate_mode="hybrid",
    )
    dense_rescue_triggered = dense_rescue_enabled and len(hybrid_candidates) < _FOLDER_CANDIDATE_FLOOR
    dense_candidates: list[Candidate] = []
    dense_recall_ms = 0.0
    if dense_rescue_triggered:
        dense_started_at = time.monotonic()
        dense_candidates = await _folder_lane_candidates(
            tenant_id,
            question,
            catalog,
            folder_id,
            quota,
            candidate_mode="dense",
        )
        dense_recall_ms = round((time.monotonic() - dense_started_at) * 1000, 2)

    candidates = _merge_candidate_lanes(hybrid_candidates, dense_candidates)
    return FolderCandidatePool(
        candidates=candidates,
        hybrid_candidate_count=len(hybrid_candidates),
        dense_rescue_triggered=dense_rescue_triggered,
        dense_candidate_count=len(dense_candidates),
        dense_recall_ms=dense_recall_ms,
    )


def _rerank_model(catalog: Catalog) -> LLMBundle:
    exists, knowledgebase = KnowledgebaseService.get_by_id(catalog.dataset_id)
    if not exists:
        raise RuntimeError("DocMind catalog dataset is unavailable")
    rerank_id = os.environ.get("DOCMIND_RERANK_ID", _DEFAULT_RERANK_ID)
    model_config = get_model_config_from_provider_instance(
        knowledgebase.tenant_id,
        LLMType.RERANK,
        rerank_id,
    )
    return LLMBundle(knowledgebase.tenant_id, model_config)


async def search(
    tenant_id: str,
    question: str,
    *,
    folder_ids: object | None = None,
    catalog_version_id: str | None = None,
    dense_rescue_enabled: bool = True,
) -> dict[str, Any]:
    catalog = _load_catalog()
    if not KnowledgebaseService.accessible(catalog.dataset_id, tenant_id):
        raise PermissionError("DocMind catalog dataset is not accessible")
    trace_id = str(uuid4())
    started_at = time.monotonic()
    if catalog.source == "static":
        docmind_catalog_shadow_service.maybe_enqueue_catalog_shadow(
            catalog, tenant_id, trace_id
        )
    current_catalog_version_id = _catalog_version_id(catalog)
    if folder_ids is None:
        if catalog_version_id is not None:
            raise ValueError("catalog_version_id requires manual folder_ids")
        scope_mode = "automatic"
        routed_folders, routing_metadata = await _route_folders(tenant_id, question, catalog, trace_id)
    else:
        if catalog_version_id is not None and catalog_version_id != current_catalog_version_id:
            raise ValueError("DOCMIND_FOLDER_SELECTION_STALE: refresh folders")
        scope_mode = "manual"
        routed_folders = _select_manual_folders(catalog, folder_ids)
        routing_metadata = {"method": "manual", "cache": "bypassed"}
    selected_folders = _expand_document_folders(catalog, routed_folders)
    routed_at = time.monotonic()
    if not selected_folders:
        timings_ms = {
            "routing": round((routed_at - started_at) * 1000, 2),
            "recall": 0.0,
            "dense_recall": 0.0,
            "rerank": 0.0,
            "total": round((routed_at - started_at) * 1000, 2),
        }
        return {
            "dataset_id": catalog.dataset_id,
            "chunks": [],
            "ranked_chunks": [],
            "ranked_total": 0,
            "total": 0,
            "candidate_count": 0,
            "selected_folders": [],
            "scope_doc_ids": [],
            "trace_id": trace_id,
            "folder_routing": routing_metadata,
            "routing_policy": _routing_policy(
                dense_rescue_enabled=dense_rescue_enabled,
                hierarchical=_is_hierarchical(catalog),
                method=routing_metadata["method"],
            ),
            "timings_ms": timings_ms,
            "catalog_source": catalog.source,
            "catalog_version_id": current_catalog_version_id,
            "scope_mode": scope_mode,
            "effective_folder_ids": [],
            "routed_folder_ids": [row["id"] for row in routed_folders],
            "caps": _search_caps(catalog),
        }
    dense_rescue_eligibility = {
        folder["id"]: dense_rescue_enabled and index == 0
        for index, folder in enumerate(selected_folders)
    }

    candidate_sets = await asyncio.wait_for(
        asyncio.gather(
            *[
                _folder_candidates(
                    tenant_id,
                    question,
                    catalog,
                    folder["id"],
                    min(_FOLDER_CANDIDATE_LIMIT, len(catalog.folders[folder["id"]]) * _DOCUMENT_CANDIDATE_LIMIT),
                    dense_rescue_enabled=dense_rescue_eligibility[folder["id"]],
                )
                for folder in selected_folders
            ]
        ),
        timeout=_RECALL_TIMEOUT_SECONDS,
    )
    folder_candidate_pools = {folder["id"]: pool for folder, pool in zip(selected_folders, candidate_sets, strict=True)}
    folder_pools = {folder_id: pool.candidates for folder_id, pool in folder_candidate_pools.items()}
    seen_chunks: set[str] = set()
    global_document_counts: dict[str, int] = {}
    for folder in selected_folders:
        deduplicated_pool: list[Candidate] = []
        for candidate in folder_pools[folder["id"]]:
            chunk_id = str(candidate.chunk.get("chunk_id") or candidate.chunk.get("id") or "")
            doc_id = str(candidate.chunk.get("doc_id") or "")
            if not chunk_id or chunk_id in seen_chunks or global_document_counts.get(doc_id, 0) >= _DOCUMENT_CANDIDATE_LIMIT:
                continue
            seen_chunks.add(chunk_id)
            global_document_counts[doc_id] = global_document_counts.get(doc_id, 0) + 1
            deduplicated_pool.append(candidate)
        folder_pools[folder["id"]] = deduplicated_pool

    folder_document_counts = {folder["id"]: len(catalog.folders[folder["id"]]) for folder in selected_folders}
    quotas = _allocate_folder_quotas(
        selected_folders,
        folder_document_counts,
        folder_candidate_capacities={folder_id: len(pool) for folder_id, pool in folder_pools.items()},
    )
    candidates = [candidate for folder in selected_folders for candidate in folder_pools[folder["id"]][: quotas[folder["id"]]]]
    if len(candidates) > _RERANK_CANDIDATE_LIMIT:
        raise RuntimeError("DocMind candidate budget exceeded")
    allowed_doc_ids = {doc_id for folder in selected_folders for doc_id in catalog.folders[folder["id"]]}
    for candidate in candidates:
        if candidate.chunk.get("doc_id") not in catalog.folders[candidate.folder_id]:
            raise RuntimeError("DocMind candidate escaped the selected catalog scope")
        if candidate.chunk.get("kb_id") not in (None, "", catalog.dataset_id):
            raise RuntimeError("DocMind candidate escaped the catalog dataset")
    for folder in selected_folders:
        candidate_pool = folder_candidate_pools[folder["id"]]
        folder["allocated_quota"] = quotas[folder["id"]]
        folder["actual_candidate_count"] = min(len(folder_pools[folder["id"]]), quotas[folder["id"]])
        folder["hybrid_candidate_count"] = candidate_pool.hybrid_candidate_count
        folder["dense_rescue_eligible"] = dense_rescue_eligibility[folder["id"]]
        folder["dense_rescue_triggered"] = candidate_pool.dense_rescue_triggered
        folder["dense_candidate_count"] = candidate_pool.dense_candidate_count
        folder["dense_recall_ms"] = candidate_pool.dense_recall_ms
        folder["merged_candidate_count"] = len(candidate_pool.candidates)
    recalled_at = time.monotonic()
    scope_doc_ids = [
        doc_id
        for folder in selected_folders
        for doc_id in catalog.folders[folder["id"]]
    ]
    response_scope = {
        "folder_routing": routing_metadata,
        "dataset_id": catalog.dataset_id,
        "catalog_source": catalog.source,
        "catalog_version_id": current_catalog_version_id,
        "scope_mode": scope_mode,
        "effective_folder_ids": [folder["id"] for folder in selected_folders],
        "routed_folder_ids": [folder["id"] for folder in routed_folders],
        "caps": _search_caps(catalog),
    }
    if not candidates:
        timings_ms = {
            "routing": round((routed_at - started_at) * 1000, 2),
            "recall": round((recalled_at - routed_at) * 1000, 2),
            "dense_recall": max((pool.dense_recall_ms for pool in folder_candidate_pools.values()), default=0.0),
            "rerank": 0.0,
            "total": round((recalled_at - started_at) * 1000, 2),
        }
        return {
            "chunks": [],
            "ranked_chunks": [],
            "ranked_total": 0,
            "total": 0,
            "candidate_count": 0,
            "selected_folders": selected_folders,
            "scope_doc_ids": scope_doc_ids,
            "trace_id": trace_id,
            "routing_policy": _routing_policy(
                dense_rescue_enabled=dense_rescue_enabled,
                hierarchical=_is_hierarchical(catalog),
                method=routing_metadata["method"],
            ),
            "timings_ms": timings_ms,
            **response_scope,
        }

    reranker = _rerank_model(catalog)
    scores, used_tokens = await asyncio.wait_for(
        asyncio.to_thread(reranker.similarity, question, [candidate.rerank_text for candidate in candidates]),
        timeout=_RERANK_TIMEOUT_SECONDS,
    )
    if len(scores) != len(candidates) or any(not math.isfinite(float(score)) for score in scores):
        raise RuntimeError("DocMind reranker returned invalid scores")
    reranked_at = time.monotonic()
    ranked_indices = sorted(range(len(candidates)), key=lambda index: (-float(scores[index]), index))
    ranked_chunks: list[dict[str, Any]] = []
    for index in ranked_indices:
        chunk = _enrich_parser_platform_chunk(dict(candidates[index].chunk))
        chunk["similarity"] = float(scores[index])
        chunk["rerank_score"] = float(scores[index])
        chunk["folder_id"] = candidates[index].folder_id
        if chunk.get("doc_id") not in allowed_doc_ids:
            raise RuntimeError("DocMind result escaped the selected catalog scope")
        ranked_chunks.append(chunk)
    relative_paths = await asyncio.to_thread(document_relative_paths, catalog.dataset_id, {chunk["doc_id"] for chunk in ranked_chunks})
    for chunk in ranked_chunks:
        chunk.pop("document_relative_path", None)
        if relative_path := relative_paths.get(chunk["doc_id"]):
            chunk["document_relative_path"] = relative_path
    chunks = ranked_chunks[:_FINAL_RESULT_LIMIT]

    timings_ms = {
        "routing": round((routed_at - started_at) * 1000, 2),
        "recall": round((recalled_at - routed_at) * 1000, 2),
        "dense_recall": max((pool.dense_recall_ms for pool in folder_candidate_pools.values()), default=0.0),
        "rerank": round((reranked_at - recalled_at) * 1000, 2),
        "total": round((reranked_at - started_at) * 1000, 2),
    }
    logger.info(
        "DocMind search trace=%s scope_mode=%s folders=%d candidates=%d quota_total=%d quota_max=%d "
        "hybrid_candidates=%d dense_candidates=%d dense_rescues=%d timings_ms=%s",
        trace_id,
        scope_mode,
        len(selected_folders),
        len(candidates),
        sum(quotas.values()),
        max(quotas.values(), default=0),
        sum(pool.hybrid_candidate_count for pool in folder_candidate_pools.values()),
        sum(pool.dense_candidate_count for pool in folder_candidate_pools.values()),
        sum(pool.dense_rescue_triggered for pool in folder_candidate_pools.values()),
        timings_ms,
    )
    logger.info(
        "DocMind result trace=%s scope_mode=%s result_count=%d ranked_count=%d rerank_tokens=%s",
        trace_id,
        scope_mode,
        len(chunks),
        len(ranked_chunks),
        used_tokens,
    )
    return {
        "chunks": chunks,
        "ranked_chunks": ranked_chunks,
        "ranked_total": len(ranked_chunks),
        "total": len(chunks),
        "candidate_count": len(candidates),
        "selected_folders": selected_folders,
        "scope_doc_ids": scope_doc_ids,
        "trace_id": trace_id,
        "routing_policy": _routing_policy(
            dense_rescue_enabled=dense_rescue_enabled,
            hierarchical=_is_hierarchical(catalog),
            method=routing_metadata["method"],
        ),
        "timings_ms": timings_ms,
        "rerank_used_tokens": used_tokens,
        **response_scope,
    }


def _routing_policy(
    *,
    dense_rescue_enabled: bool = True,
    hierarchical: bool = False,
    method: str = "vector",
) -> dict[str, Any]:
    policy = {
        "min_folders": _MIN_FOLDER_COUNT,
        "max_folders": (
            _MAX_DOCUMENT_BEARING_FOLDER_COUNT
            if hierarchical
            else _MAX_FOLDER_COUNT
        ),
        "routing_window": _ROUTING_WINDOW_LIMIT,
        "cumulative_probability": _CUMULATIVE_PROBABILITY_THRESHOLD,
        "softmax_temperature": _ROUTING_SOFTMAX_TEMPERATURE,
        "ambiguous_margin": _ROUTING_AMBIGUOUS_MARGIN,
        "very_ambiguous_margin": _ROUTING_VERY_AMBIGUOUS_MARGIN,
        "low_score_floor": _ROUTING_LOW_SCORE_FLOOR,
        "no_catalog_fallback": "all_accessible_catalog_folders",
        "candidate_budget": _RERANK_CANDIDATE_LIMIT,
        "folder_floor": _FOLDER_CANDIDATE_FLOOR,
        "folder_cap": _FOLDER_CANDIDATE_LIMIT,
        "document_cap": _DOCUMENT_CANDIDATE_LIMIT,
        "dense_rescue_enabled": dense_rescue_enabled,
        "dense_rescue_scope": "primary_folder",
        "dense_rescue_threshold": _FOLDER_CANDIDATE_FLOOR,
    }
    if method != "vector":
        for key in ("routing_window", "cumulative_probability", "softmax_temperature", "ambiguous_margin", "very_ambiguous_margin", "low_score_floor", "no_catalog_fallback"):
            policy.pop(key)
        policy["selection_method"] = method
        if method == "llm":
            policy["routing_max_folders"] = docmind_llm_router_service.MAX_FOLDERS
            policy["quota_weighting"] = "reciprocal_rank_not_confidence"
    return policy
