"""Read the authorized cloud-source tree directly from active index records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from api.db.db_models import (
    DocmindIngestionJob,
    DocmindProject,
    DocmindSource,
    DocmindSourceDocument,
    DocmindSourceVersion,
    Document,
    ParserRun,
)
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.docmind_source_path import normalize_logical_relative_path


@dataclass(frozen=True)
class SourceProjection:
    project_id: str
    dataset_id: str
    folders: dict[str, tuple[str, ...]]
    folder_tree: tuple[dict[str, Any], ...]
    document_paths: dict[str, str]
    document_names: dict[str, str]
    document_version_ids: dict[str, str]
    document_parser_run_ids: dict[str, str]
    document_chunk_set_ids: dict[str, str]
    hierarchy_nodes: tuple[dict[str, Any], ...]
    version_id: str
    root_id: str


def _id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def _display_path(source_name: str, relative_path: str) -> str:
    return source_name.rstrip("\\/") + "\\" + relative_path.replace("/", "\\")


def _records_by_id(model, ids: set[str]) -> dict[str, Any]:
    records: dict[str, Any] = {}
    ordered = sorted(ids)
    for offset in range(0, len(ordered), 500):
        for record in model.select().where(model.id.in_(ordered[offset : offset + 500])):
            records[record.id] = record
    return records


def _searchable(mapping, version, job, run, document, project_id: str) -> bool:
    return bool(
        mapping.deleted_at is None
        and mapping.active_source_version_id
        and version is not None
        and version.id == mapping.active_source_version_id
        and version.source_document_id == mapping.id
        and version.document_id == mapping.document_id
        and version.lifecycle_state == "ACTIVE"
        and version.parser_run_id
        and version.chunk_set_id
        and version.search_cleanup_complete
        and job is not None
        and job.project_id == project_id
        and job.source_id == mapping.source_id
        and job.source_document_id == mapping.id
        and job.document_id == mapping.document_id
        and job.version_id == version.id
        and run is not None
        and run.id == version.parser_run_id
        and run.doc_id == mapping.document_id
        and run.chunk_set_id == version.chunk_set_id
        and run.lifecycle in {"READY", "READY_WITH_WARNING"}
        and run.raw_artifact_ref is None
        and int(run.staged_chunk_count or 0) > 0
        and document is not None
        and document.id == mapping.document_id
        and document.active_chunk_set_id == version.chunk_set_id
        and str(document.status) == "1"
    )


def _index_status(job, *, searchable: bool, run=None) -> dict[str, Any]:
    """Describe ingestion separately from access to a previously active index."""
    state = job.lifecycle_state if job is not None else None
    cleanup_states = {job.cleanup_state, job.host_cleanup_state} if job is not None else set()
    cleanup = (
        "FAILED" if "FAILED" in cleanup_states else
        "PENDING" if cleanup_states & {"PENDING", "IN_PROGRESS"} else
        "COMPLETE" if cleanup_states == {"COMPLETE"} else None
    )
    if state in {"FAILED", "ACTION_REQUIRED", "CLEANUP_FAILED", "RETRY_WAIT", "CLEANUP"}:
        index_state = state
    elif state in {"DECRYPTING", "PARSING", "INDEXING"}:
        index_state = "PROCESSING"
    elif state in {"DISCOVERED", "WAITING_SOURCE_STABLE"}:
        index_state = "PENDING"
    elif searchable:
        index_state = "INDEXED"
    elif state == "COMPLETE":
        # A completed job alone is not evidence of a usable index.
        index_state = "FAILED"
    else:
        index_state = "PENDING" if state is None else "FAILED"
    error_code = job.error_code if job is not None else None
    # Worker-provided messages/codes can contain paths or arbitrary data.
    if error_code:
        allowed_errors = {
            "HOST_WORKER_ERROR", "DOCMIND_INGESTION_FAILED", "DOCMIND_INGESTION_INTERRUPTED",
            "DOCMIND_INGESTION_CLEANUP_FAILED", "DOCMIND_INGESTION_STAGING_CLEANUP_FAILED",
            "DOCMIND_INGESTION_SOURCE_CHANGED", "DOCMIND_INGESTION_RETRY_EXHAUSTED",
            "EPHEMERAL_CLEANUP_FAILED", "EPHEMERAL_CLEANUP_STATE_FAILED",
        }
        if error_code not in allowed_errors:
            error_code = "DOCMIND_INGESTION_FAILED"
    native_warnings = set(run.warnings or ()) if searchable and run is not None and run.parser_name == "pptx-native" else set()
    return {
        "index_state": index_state,
        "index_cleanup_state": cleanup,
        "index_error_code": error_code,
        "searchable": searchable,
        "index_partial_coverage": "PPTX_NATIVE_PARTIAL_COVERAGE" in native_warnings,
        "index_image_ocr_not_run": "IMAGE_OCR_NOT_RUN" in native_warnings,
    }


def project_for_tenant(tenant_id: str) -> DocmindProject | None:
    projects = list(
        DocmindProject.select()
        .where(
            (DocmindProject.tenant_id == tenant_id)
            & (DocmindProject.catalog_source_mode == "database")
        )
        .limit(2)
    )
    if len(projects) > 1:
        raise RuntimeError("DOCMIND_PROJECT_SERVING_AMBIGUOUS")
    return projects[0] if projects else None


def load(tenant_id: str) -> SourceProjection | None:
    project = project_for_tenant(tenant_id)
    if project is None:
        return None
    if not KnowledgebaseService.accessible(project.dataset_id, tenant_id):
        raise PermissionError("DocMind source dataset is not accessible")

    sources = list(
        DocmindSource.select()
        .where(DocmindSource.project_id == project.id)
        .order_by(DocmindSource.id)
    )
    by_source = {source.id: source for source in sources if source.enabled}
    mappings = list(
        DocmindSourceDocument.select()
        .where(
            (DocmindSourceDocument.project_id == project.id)
            & (DocmindSourceDocument.source_id.in_(list(by_source)))
            & (DocmindSourceDocument.deleted_at.is_null(True))
        )
        .order_by(DocmindSourceDocument.source_id, DocmindSourceDocument.relative_path, DocmindSourceDocument.id)
    ) if by_source else []
    versions = _records_by_id(
        DocmindSourceVersion,
        {row.active_source_version_id for row in mappings if row.active_source_version_id},
    )
    jobs: dict[str, DocmindIngestionJob] = {}
    version_ids = sorted(versions)
    for offset in range(0, len(version_ids), 500):
        for job in DocmindIngestionJob.select().where(
            (DocmindIngestionJob.project_id == project.id)
            & (DocmindIngestionJob.version_id.in_(version_ids[offset : offset + 500]))
        ):
            jobs[job.version_id] = job
    runs = _records_by_id(
        ParserRun,
        {version.parser_run_id for version in versions.values() if version.parser_run_id},
    )
    documents = _records_by_id(Document, {row.document_id for row in mappings})
    latest_jobs: dict[str, DocmindIngestionJob] = {}
    mappings_by_id = {row.id: row for row in mappings}
    mapping_ids = sorted(mappings_by_id)
    for offset in range(0, len(mapping_ids), 500):
        query = DocmindIngestionJob.select(
            DocmindIngestionJob.id, DocmindIngestionJob.source_document_id,
            DocmindIngestionJob.source_id, DocmindIngestionJob.document_id,
            DocmindIngestionJob.lifecycle_state, DocmindIngestionJob.cleanup_state,
            DocmindIngestionJob.host_cleanup_state, DocmindIngestionJob.error_code,
        ).where(
            (DocmindIngestionJob.project_id == project.id)
            & (DocmindIngestionJob.source_document_id.in_(mapping_ids[offset : offset + 500]))
        ).order_by(DocmindIngestionJob.create_time.desc(), DocmindIngestionJob.id.desc())
        for candidate in query.iterator():
            mapping = mappings_by_id[candidate.source_document_id]
            if candidate.source_id == mapping.source_id and candidate.document_id == mapping.document_id:
                latest_jobs.setdefault(mapping.id, candidate)

    root_id = _id("docmind-source-root", project.id)
    folder_rows: dict[str, dict[str, Any]] = {}
    folder_documents: dict[str, list[str]] = {}
    nodes: dict[str, dict[str, Any]] = {}
    document_paths: dict[str, str] = {}
    document_names: dict[str, str] = {}
    document_version_ids: dict[str, str] = {}
    document_parser_run_ids: dict[str, str] = {}
    document_chunk_set_ids: dict[str, str] = {}
    version_members: list[tuple[str, str, str | None, bool]] = []

    def add_folder(folder_id: str, parent_id: str | None, name: str, path: str, depth: int) -> None:
        if folder_id in folder_rows:
            return
        folder_rows[folder_id] = {
            "id": folder_id,
            "parent_id": parent_id,
            "name": name,
            "relative_path": path,
            "depth": depth,
            "ordinal": len(folder_rows),
        }
        folder_documents[folder_id] = []
        nodes[folder_id] = {
            "file_id": folder_id,
            "parent_file_id": parent_id,
            "name": name,
            "relative_path": path,
            "depth": depth,
            "type": "folder",
            "child_count": 0,
            "semantic_folder_id": None,
            "mutation_capabilities": {
                "can_create_child": False,
                "can_edit": False,
                "can_delete": False,
                "edit_blocker_code": "SOURCE_WRITE_UNVERIFIED",
                "delete_blocker_code": "SOURCE_WRITE_UNVERIFIED",
            },
        }
        if parent_id is not None:
            nodes[parent_id]["child_count"] += 1

    add_folder(root_id, None, "DocMind", "", 0)
    for source in sources:
        source_root_id = _id("docmind-source-folder", project.id, source.id, "")
        if source.enabled:
            add_folder(source_root_id, root_id, source.display_name, source.display_name, 1)
            nodes[source_root_id]["source_enabled"] = True
            nodes[source_root_id]["sync_state"] = "ACTIVE"
        else:
            nodes[source_root_id] = {
                "file_id": source_root_id,
                "parent_file_id": root_id,
                "name": source.display_name,
                "relative_path": source.display_name,
                "depth": 1,
                "type": "folder",
                "child_count": 0,
                "source_enabled": False,
                "sync_state": "PAUSED",
                "semantic_folder_id": None,
                "mutation_capabilities": {
                    "can_create_child": False,
                    "can_edit": False,
                    "can_delete": False,
                    "edit_blocker_code": "SOURCE_WRITE_UNVERIFIED",
                    "delete_blocker_code": "SOURCE_WRITE_UNVERIFIED",
                },
            }
            nodes[root_id]["child_count"] += 1

    for mapping in mappings:
        source = by_source[mapping.source_id]
        try:
            path = normalize_logical_relative_path(mapping.relative_path)
        except ValueError as error:
            raise RuntimeError("DOCMIND_SOURCE_PATH_INVALID") from error
        parts = PurePosixPath(path).parts
        version = versions.get(mapping.active_source_version_id)
        job = jobs.get(version.id) if version is not None else None
        run = runs.get(version.parser_run_id) if version is not None else None
        document = documents.get(mapping.document_id)
        eligible = _searchable(mapping, version, job, run, document, project.id)
        if document is not None and document.kb_id != project.dataset_id:
            eligible = False
        version_members.append((mapping.id, path, version.id if version is not None else None, eligible))

        parent_id = _id("docmind-source-folder", project.id, source.id, "")
        for depth, index in enumerate(range(1, len(parts)), start=2):
            relative_folder = "/".join(parts[:index])
            folder_id = _id("docmind-source-folder", project.id, source.id, relative_folder)
            display_path = _display_path(source.display_name, relative_folder)
            add_folder(folder_id, parent_id, parts[index - 1], display_path, depth)
            parent_id = folder_id

        display_path = _display_path(source.display_name, path)
        nodes[_id("docmind-source-document-node", mapping.id)] = {
            "file_id": _id("docmind-source-document-node", mapping.id),
            "parent_file_id": parent_id,
            "name": parts[-1],
            "relative_path": display_path,
            "depth": len(parts) + 1,
            "type": "file",
            "document_id": mapping.document_id,
            "document_exists": document is not None,
            **_index_status(latest_jobs.get(mapping.id), searchable=eligible, run=run),
        }
        nodes[parent_id]["child_count"] += 1
        if eligible:
            folder_documents[parent_id].append(mapping.document_id)
            document_paths[mapping.document_id] = display_path
            document_names[mapping.document_id] = parts[-1]
            document_version_ids[mapping.document_id] = version.id
            document_parser_run_ids[mapping.document_id] = version.parser_run_id
            document_chunk_set_ids[mapping.document_id] = version.chunk_set_id

    fingerprint = hashlib.sha256(
        json.dumps(
            [project.id, [(source.id, source.display_name, source.enabled) for source in sources], version_members],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return SourceProjection(
        project_id=project.id,
        dataset_id=project.dataset_id,
        folders={key: tuple(value) for key, value in folder_documents.items()},
        folder_tree=tuple(folder_rows.values()),
        document_paths=document_paths,
        document_names=document_names,
        document_version_ids=document_version_ids,
        document_parser_run_ids=document_parser_run_ids,
        document_chunk_set_ids=document_chunk_set_ids,
        hierarchy_nodes=tuple(nodes.values()),
        version_id=f"sources-{fingerprint}",
        root_id=root_id,
    )
