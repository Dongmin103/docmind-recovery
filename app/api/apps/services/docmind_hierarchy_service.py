from __future__ import annotations

import hashlib
import inspect
import json
import logging
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

import xxhash
from peewee import IntegrityError, fn

from api.apps.services import (
    docmind_canary_policy,
    docmind_draft_service,
    docmind_registration_service,
)
from api.db import FileType
from api.db.db_models import (
    DocmindCatalogVersion,
    DocmindDraftChange,
    DocmindFolder,
    DocmindFolderImportItem,
    DocmindFolderImportJob,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindProject,
    DocmindRegistration,
    DocmindRegistrationCurrent,
    Document,
    File,
    File2Document,
)
from api.db.services.document_service import (
    DOCMIND_PROTECTED_LIFECYCLES,
    DocmindProtectedEvidenceError,
    DocumentService,
)
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.constants import TaskStatus
from common.misc_utils import get_uuid, thread_pool_exec
from common.time_utils import current_timestamp

logger = logging.getLogger(__name__)

MAX_IMPORT_FILES = 100
MAX_IMPORT_DEPTH = 12
MAX_IMPORT_FILE_BYTES = 100 * 1024 * 1024
MAX_IMPORT_TOTAL_BYTES = 500 * 1024 * 1024
SOURCE_TYPE = "docmind_source"
MUTATION_CAPABILITY_QUERY_BATCH_SIZE = 500


class DocmindHierarchyError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class _BufferedFile:
    id: str
    filename: str
    blob: bytes
    fingerprint: str

    def read(self) -> bytes:
        return self.blob


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _stable_id(*parts: str) -> str:
    return _hash("\x1f".join(parts))[:32]


def _name_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


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


def _context(tenant_id: str):
    try:
        return docmind_registration_service._owner_context(tenant_id)
    except docmind_registration_service.DocmindRegistrationError as error:
        raise DocmindHierarchyError(error.code) from error


def normalize_relative_path(raw: str) -> str:
    normalized = unicodedata.normalize("NFC", str(raw or "").replace("\\", "/"))
    if not normalized or normalized.startswith("/"):
        raise DocmindHierarchyError("DOCMIND_IMPORT_PATH_INVALID")
    normalized = normalized.strip("/")
    parts = normalized.split("/")
    if (
        len(parts) > MAX_IMPORT_DEPTH + 1
        or any(not part or part in {".", ".."} or "\x00" in part for part in parts)
        or any(len(part) > 255 for part in parts)
        or len(normalized) > 1024
    ):
        raise DocmindHierarchyError("DOCMIND_IMPORT_PATH_INVALID")
    path = PurePosixPath(*parts).as_posix()
    if path.startswith("../") or "/../" in path:
        raise DocmindHierarchyError("DOCMIND_IMPORT_PATH_INVALID")
    return path


def _source_root(context: Any) -> File:
    project = DocmindProject.get_by_id(context.project.id)
    if project.source_root_file_id:
        exists, row = FileService.get_by_id(project.source_root_file_id)
        if (
            not exists
            or row is None
            or row.tenant_id != context.project.tenant_id
            or row.type != FileType.FOLDER.value
        ):
            raise DocmindHierarchyError("DOCMIND_SOURCE_ROOT_INVALID")
        return row
    exists, kb = KnowledgebaseService.get_by_id(context.project.dataset_id)
    if not exists:
        raise DocmindHierarchyError("DOCMIND_DATASET_NOT_FOUND")
    root = FileService.get_root_folder(context.project.tenant_id)
    FileService.init_knowledgebase_docs(root["id"], context.project.tenant_id)
    kb_root = FileService.get_kb_folder(context.project.tenant_id)
    dataset_folder = FileService.new_a_file_from_kb(
        context.project.tenant_id,
        kb.name,
        kb_root["id"],
    )
    changed = (
        DocmindProject.update(
            source_root_file_id=dataset_folder["id"],
            **_updates(),
        )
        .where(
            (DocmindProject.id == project.id)
            & (DocmindProject.source_root_file_id.is_null(True))
        )
        .execute()
    )
    project = DocmindProject.get_by_id(project.id)
    if changed not in {0, 1} or not project.source_root_file_id:
        raise DocmindHierarchyError("DOCMIND_SOURCE_ROOT_CONFLICT")
    exists, row = FileService.get_by_id(project.source_root_file_id)
    if not exists or row is None:
        raise DocmindHierarchyError("DOCMIND_SOURCE_ROOT_INVALID")
    return row


def _children(parent_id: str) -> list[File]:
    return sorted(
        FileService.list_all_files_by_parent_id(parent_id),
        key=lambda row: (row.type != FileType.FOLDER.value, row.name.casefold(), row.id),
    )


def _tree_rows(root: File, dataset_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def visit(folder: File, relative_path: str, depth: int) -> None:
        children = _children(folder.id)
        rows.append(
            {
                "file_id": folder.id,
                "parent_file_id": None if folder.id == root.id else folder.parent_id,
                "name": folder.name,
                "relative_path": relative_path,
                "depth": depth,
                "type": "folder",
                "child_count": len(children),
            }
        )
        for child in children:
            child_path = f"{relative_path}/{child.name}" if relative_path else child.name
            if child.type == FileType.FOLDER.value:
                visit(child, child_path, depth + 1)
            else:
                links = File2DocumentService.get_by_file_id(child.id)
                document_id = links[0].document_id if links else None
                document = (
                    Document.get_or_none(
                        (Document.id == document_id)
                        & (Document.kb_id == dataset_id)
                    )
                    if document_id
                    else None
                )
                rows.append(
                    {
                        "file_id": child.id,
                        "parent_file_id": child.parent_id,
                        "name": child.name,
                        "relative_path": child_path,
                        "depth": depth + 1,
                        "type": "file",
                        "document_id": document_id,
                        "document_exists": document is not None,
                        "index_state": (
                            "INDEXED"
                            if document is not None
                            and str(document.run) == TaskStatus.DONE.value
                            and float(document.progress or 0) == 1.0
                            and int(document.chunk_num or 0) > 0
                            else "PENDING"
                        ),
                    }
                )

    visit(root, "", 0)
    return rows


def current_source_tree_hash(tenant_id: str) -> str:
    context = _context(tenant_id)
    root = _source_root(context)
    projection = [
        {
            "file_id": row["file_id"],
            "parent_file_id": row["parent_file_id"],
            "relative_path": row["relative_path"],
            "type": row["type"],
            "document_id": row.get("document_id"),
        }
        for row in _tree_rows(root, context.project.dataset_id)
    ]
    return _hash(_json(projection))


def _next_folder_ordinal(project_id: str) -> int:
    value = (
        DocmindFolder.select(fn.MAX(DocmindFolder.ordinal))
        .where(DocmindFolder.project_id == project_id)
        .scalar()
    )
    return int(value if value is not None else -1) + 1


def _semantic_folder(
    project: DocmindProject,
    source_folder: File,
    relative_path: str,
    display_name: str,
) -> DocmindFolder:
    existing = DocmindFolder.get_or_none(
        (DocmindFolder.project_id == project.id)
        & (DocmindFolder.source_file_id == source_folder.id)
    )
    if existing:
        if existing.display_name != display_name:
            DocmindFolder.update(
                display_name=display_name,
                **_updates(),
            ).where(DocmindFolder.id == existing.id).execute()
            existing = DocmindFolder.get_by_id(existing.id)
        return existing
    semantic_id = _stable_id("docmind-semantic-folder", project.id, source_folder.id)
    try:
        return DocmindFolder.create(
            id=semantic_id,
            project_id=project.id,
            slug=f"node-{semantic_id[:16]}",
            display_name=display_name,
            ordinal=_next_folder_ordinal(project.id),
            enabled=True,
            source_file_id=source_folder.id,
            **_timestamps(),
        )
    except IntegrityError as error:
        raise DocmindHierarchyError("DOCMIND_SEMANTIC_FOLDER_CONFLICT") from error


def _ensure_folder(parent: File, name: str, tenant_id: str) -> File:
    matches = [
        child
        for child in _children(parent.id)
        if _name_key(child.name) == _name_key(name)
    ]
    if matches:
        if len(matches) != 1 or matches[0].type != FileType.FOLDER.value:
            raise DocmindHierarchyError("DOCMIND_IMPORT_PATH_CONFLICT")
        return matches[0]
    folder = FileService.insert(
        {
            "id": get_uuid(),
            "parent_id": parent.id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": name,
            "location": "",
            "size": 0,
            "type": FileType.FOLDER.value,
            "source_type": SOURCE_TYPE,
        }
    )
    return folder


def _folder_chain(root: File, parts: tuple[str, ...], tenant_id: str) -> list[File]:
    current = root
    result = [root]
    for part in parts:
        current = _ensure_folder(current, part, tenant_id)
        result.append(current)
    return result


def _public_job(
    job: DocmindFolderImportJob,
    *,
    dataset_id: str,
) -> dict[str, Any]:
    items = list(
        DocmindFolderImportItem.select()
        .where(DocmindFolderImportItem.job_id == job.id)
        .order_by(DocmindFolderImportItem.create_time)
    )
    return {
        "dataset_id": dataset_id,
        "job_id": job.id,
        "state": job.lifecycle_state,
        "item_count": job.item_count,
        "succeeded_count": job.succeeded_count,
        "failed_count": job.failed_count,
        "items": [
            {
                "relative_path": item.relative_path,
                "state": item.lifecycle_state,
                "error_code": item.error_code,
                "file_id": item.source_file_id,
                "document_id": item.document_id,
                "registration_id": item.registration_id,
            }
            for item in items
        ],
    }


async def import_local_folder(
    tenant_id: str,
    raw_paths: list[str],
    file_objects: list[Any],
    idempotency_key: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    root = _source_root(context)
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or not 1 <= len(file_objects) <= MAX_IMPORT_FILES
        or len(raw_paths) != len(file_objects)
    ):
        raise DocmindHierarchyError("DOCMIND_IMPORT_REQUEST_INVALID")
    if docmind_canary_policy.enabled() and len(file_objects) != 1:
        raise DocmindHierarchyError("DOCMIND_CANARY_EXACTLY_ONE_DOCUMENT")
    normalized_paths = [normalize_relative_path(path) for path in raw_paths]
    folded = [path.casefold() for path in normalized_paths]
    if len(set(folded)) != len(folded):
        raise DocmindHierarchyError("DOCMIND_IMPORT_PATH_COLLISION")

    buffered: list[_BufferedFile] = []
    manifest_rows = []
    total_bytes = 0
    for path, file_object in zip(normalized_paths, file_objects, strict=True):
        blob = file_object.read()
        if inspect.isawaitable(blob):
            blob = await blob
        if not isinstance(blob, bytes):
            raise DocmindHierarchyError("DOCMIND_IMPORT_FILE_INVALID")
        total_bytes += len(blob)
        if (
            not blob
            or len(blob) > MAX_IMPORT_FILE_BYTES
            or total_bytes > MAX_IMPORT_TOTAL_BYTES
        ):
            raise DocmindHierarchyError("DOCMIND_IMPORT_SIZE_LIMIT")
        fingerprint = xxhash.xxh128(blob).hexdigest()
        document_id = _stable_id("docmind-import-document", context.project.id, path, fingerprint)
        buffered.append(
            _BufferedFile(
                id=document_id,
                filename=PurePosixPath(path).name,
                blob=blob,
                fingerprint=fingerprint,
            )
        )
        manifest_rows.append({"path": path, "size": len(blob), "hash": fingerprint})
    if docmind_canary_policy.enabled():
        try:
            docmind_canary_policy.validate_single_pdf(buffered[0].filename, buffered[0].blob)
        except docmind_canary_policy.DocmindCanaryPolicyError as error:
            raise DocmindHierarchyError(error.code) from error
    manifest_hash = _hash(_json(manifest_rows))
    existing_job = DocmindFolderImportJob.get_or_none(
        (DocmindFolderImportJob.project_id == context.project.id)
        & (DocmindFolderImportJob.actor_id == tenant_id)
        & (DocmindFolderImportJob.idempotency_key == idempotency_key)
    )
    if existing_job:
        if existing_job.manifest_hash != manifest_hash:
            raise DocmindHierarchyError("DOCMIND_IMPORT_IDEMPOTENCY_CONFLICT")
        return _public_job(
            existing_job,
            dataset_id=context.project.dataset_id,
        )

    exists, kb = KnowledgebaseService.get_by_id(context.project.dataset_id)
    if not exists:
        raise DocmindHierarchyError("DOCMIND_DATASET_NOT_FOUND")

    job = DocmindFolderImportJob.create(
        id=get_uuid(),
        project_id=context.project.id,
        source_root_file_id=root.id,
        actor_id=tenant_id,
        idempotency_key=idempotency_key,
        manifest_hash=manifest_hash,
        lifecycle_state="RUNNING",
        item_count=len(buffered),
        succeeded_count=0,
        failed_count=0,
        **_timestamps(),
    )
    succeeded = 0
    failed = 0
    for path, buffered_file in zip(normalized_paths, buffered, strict=True):
        item = DocmindFolderImportItem.create(
            id=get_uuid(),
            job_id=job.id,
            relative_path=path,
            path_hash=_hash(path),
            content_hash=buffered_file.fingerprint,
            lifecycle_state="RUNNING",
            error_code=None,
            **_timestamps(),
        )
        try:
            parts = PurePosixPath(path).parts
            chain = _folder_chain(root, parts[:-1], tenant_id)
            parent = chain[-1]
            for index, folder in enumerate(chain):
                relative = "/".join(parts[:index])
                _semantic_folder(context.project, folder, relative, folder.name)
            collision = next(
                (
                    child
                    for child in _children(parent.id)
                    if _name_key(child.name) == _name_key(parts[-1])
                ),
                None,
            )
            document = None
            file_row = None
            if collision:
                links = File2DocumentService.get_by_file_id(collision.id)
                document = (
                    Document.get_or_none(Document.id == links[0].document_id)
                    if links
                    else None
                )
                if document is None or document.content_hash != buffered_file.fingerprint:
                    raise DocmindHierarchyError("DOCMIND_IMPORT_PATH_CONFLICT")
                file_row = collision
            else:
                errors, uploaded = await thread_pool_exec(
                    FileService.upload_document,
                    kb,
                    [buffered_file],
                    tenant_id,
                    SOURCE_TYPE,
                    None,
                )
                if errors:
                    raise DocmindHierarchyError("DOCMIND_IMPORT_UPLOAD_FAILED")
                if len(uploaded) == 1:
                    document = Document.get_by_id(uploaded[0][0]["id"])
                elif not uploaded:
                    document = Document.get_or_none(
                        Document.id == buffered_file.id
                    )
                    if (
                        document is None
                        or document.kb_id != context.project.dataset_id
                        or document.content_hash != buffered_file.fingerprint
                    ):
                        raise DocmindHierarchyError("DOCMIND_IMPORT_UPLOAD_FAILED")
                else:
                    raise DocmindHierarchyError("DOCMIND_IMPORT_UPLOAD_FAILED")
                links = File2DocumentService.get_by_document_id(document.id)
                if len(links) != 1:
                    raise DocmindHierarchyError("DOCMIND_IMPORT_LINK_FAILED")
                file_row = File.get_by_id(links[0].file_id)
                File.update(
                    parent_id=parent.id,
                    name=parts[-1],
                    source_type=SOURCE_TYPE,
                    **_updates(),
                ).where(File.id == file_row.id).execute()
                file_row = File.get_by_id(file_row.id)

            semantic_folder = _semantic_folder(
                context.project,
                parent,
                "/".join(parts[:-1]),
                parent.name,
            )
            current = DocmindRegistrationCurrent.get_or_none(
                (DocmindRegistrationCurrent.project_id == context.project.id)
                & (DocmindRegistrationCurrent.document_id == document.id)
            )
            registration = (
                DocmindRegistration.get_by_id(current.registration_id)
                if current
                else docmind_registration_service._create_attempt(
                    context,
                    semantic_folder,
                    document.id,
                    tenant_id,
                )
            )
            if registration.lifecycle_state == "UPLOADING":
                registration = docmind_registration_service._transition(
                    registration,
                    "UPLOADED",
                    actor_id=tenant_id,
                    updates={
                        "file_id": file_row.id,
                        "captured_content_hash": document.content_hash,
                    },
                )
                await thread_pool_exec(DocumentService.run, tenant_id, document.to_dict(), {})
                registration = docmind_registration_service._transition(
                    registration,
                    "INDEX_QUEUED",
                    actor_id=tenant_id,
                )
            DocmindFolderImportItem.update(
                lifecycle_state="SUCCEEDED",
                source_file_id=file_row.id,
                document_id=document.id,
                registration_id=registration.id,
                **_updates(),
            ).where(DocmindFolderImportItem.id == item.id).execute()
            succeeded += 1
        except Exception as error:
            code = (
                error.code
                if isinstance(error, DocmindHierarchyError)
                else "DOCMIND_IMPORT_ITEM_FAILED"
            )
            if isinstance(error, DocmindHierarchyError):
                logger.warning("DocMind import item failed path=%s code=%s", path, code)
            else:
                logger.exception("DocMind import item failed path=%s code=%s", path, code)
            DocmindFolderImportItem.update(
                lifecycle_state="FAILED",
                error_code=code,
                **_updates(),
            ).where(DocmindFolderImportItem.id == item.id).execute()
            failed += 1
    state = "COMPLETED" if failed == 0 else "PARTIAL" if succeeded else "FAILED"
    DocmindFolderImportJob.update(
        lifecycle_state=state,
        succeeded_count=succeeded,
        failed_count=failed,
        **_updates(),
    ).where(DocmindFolderImportJob.id == job.id).execute()
    return _public_job(
        DocmindFolderImportJob.get_by_id(job.id),
        dataset_id=context.project.dataset_id,
    )


def list_hierarchy(tenant_id: str) -> dict[str, Any]:
    context = _context(tenant_id)
    root = _source_root(context)
    nodes = _tree_rows(root, context.project.dataset_id)
    for node in nodes:
        if node["type"] != "folder":
            continue
        exists, source_folder = FileService.get_by_id(node["file_id"])
        if not exists or source_folder is None:
            raise DocmindHierarchyError("DOCMIND_SOURCE_TREE_CHANGED")
        node["semantic_folder_id"] = _semantic_folder(
            context.project,
            source_folder,
            node["relative_path"],
            node["name"],
        ).id
    _add_folder_mutation_capabilities(nodes, root.id)
    return {
        "project_id": context.project.id,
        "dataset_id": context.project.dataset_id,
        "source_root_file_id": root.id,
        "nodes": nodes,
    }


def _add_folder_mutation_capabilities(
    nodes: list[dict[str, Any]], root_file_id: str
) -> None:
    folder_nodes = {node["file_id"]: node for node in nodes if node["type"] == "folder"}
    if not folder_nodes:
        return

    def batches(values: list[str]):
        for offset in range(0, len(values), MUTATION_CAPABILITY_QUERY_BATCH_SIZE):
            yield values[offset : offset + MUTATION_CAPABILITY_QUERY_BATCH_SIZE]

    physically_protected_folder_ids: set[str] = set()
    for folder_ids in batches(list(folder_nodes)):
        physically_protected_folder_ids.update(
            row.source_file_id
            for row in (
                DocmindFolderVersion.select(DocmindFolderVersion.source_file_id)
                .join(
                    DocmindCatalogVersion,
                    on=(DocmindFolderVersion.version_id == DocmindCatalogVersion.id),
                )
                .where(
                    (DocmindFolderVersion.source_file_id.in_(folder_ids))
                    & (
                        DocmindCatalogVersion.lifecycle_state.in_(
                            DOCMIND_PROTECTED_LIFECYCLES
                        )
                    )
                )
                .distinct()
            )
            if row.source_file_id
        )

    file_parent_ids = {
        node["file_id"]: node["parent_file_id"]
        for node in nodes
        if node["type"] == "file"
    }
    protected_file_ids: set[str] = set()
    if file_parent_ids:
        for file_ids in batches(list(file_parent_ids)):
            protected_file_ids.update(
                row.file_id
                for row in (
                    File2Document.select(File2Document.file_id)
                    .join(
                        DocmindFolderVersionDocument,
                        on=(
                            File2Document.document_id
                            == DocmindFolderVersionDocument.document_id
                        ),
                    )
                    .join(
                        DocmindCatalogVersion,
                        on=(
                            DocmindFolderVersionDocument.version_id
                            == DocmindCatalogVersion.id
                        ),
                    )
                    .where(
                        (File2Document.file_id.in_(file_ids))
                        & (
                            DocmindCatalogVersion.lifecycle_state.in_(
                                DOCMIND_PROTECTED_LIFECYCLES
                            )
                        )
                    )
                    .distinct()
                )
                if row.file_id
            )
        document_protected_folder_ids = {
            file_parent_ids[file_id]
            for file_id in protected_file_ids
            if file_id in file_parent_ids
        }
    else:
        document_protected_folder_ids = set()

    parent_ids = {
        file_id: node["parent_file_id"] for file_id, node in folder_nodes.items()
    }

    def include_ancestors(folder_ids: set[str]) -> None:
        pending = list(folder_ids)
        while pending:
            folder_id = pending.pop()
            parent_id = parent_ids.get(folder_id)
            if parent_id is not None and parent_id not in folder_ids:
                folder_ids.add(parent_id)
                pending.append(parent_id)

    include_ancestors(physically_protected_folder_ids)
    include_ancestors(document_protected_folder_ids)

    for file_id, node in folder_nodes.items():
        if file_id == root_file_id:
            edit_blocker = "DOCMIND_SOURCE_ROOT_IMMUTABLE"
            delete_blocker = "DOCMIND_SOURCE_ROOT_IMMUTABLE"
        elif file_id in physically_protected_folder_ids:
            edit_blocker = "DOCMIND_FOLDER_PHYSICALLY_PROTECTED"
            delete_blocker = "DOCMIND_FOLDER_PHYSICALLY_PROTECTED"
        elif file_id in document_protected_folder_ids:
            edit_blocker = "DOCMIND_VERSION_PROTECTED"
            delete_blocker = "DOCMIND_VERSION_PROTECTED"
        else:
            edit_blocker = None
            delete_blocker = (
                "DOCMIND_FOLDER_DELETE_NONEMPTY" if node["child_count"] else None
            )
        node["mutation_capabilities"] = {
            "can_create_child": True,
            "can_edit": edit_blocker is None,
            "can_delete": delete_blocker is None,
            "edit_blocker_code": edit_blocker,
            "delete_blocker_code": delete_blocker,
        }


def list_import_jobs(tenant_id: str) -> dict[str, Any]:
    context = _context(tenant_id)
    jobs = list(
        DocmindFolderImportJob.select()
        .where(DocmindFolderImportJob.project_id == context.project.id)
        .order_by(DocmindFolderImportJob.create_time.desc())
        .limit(20)
    )
    return {
        "dataset_id": context.project.dataset_id,
        "jobs": [
            _public_job(job, dataset_id=context.project.dataset_id)
            for job in jobs
        ],
    }


def create_folder(tenant_id: str, parent_file_id: str, name: str) -> dict[str, Any]:
    context = _context(tenant_id)
    root = _source_root(context)
    normalized_name = unicodedata.normalize("NFC", str(name or "")).strip()
    if (
        not normalized_name
        or normalized_name in {".", ".."}
        or "/" in normalized_name
        or "\\" in normalized_name
        or "\x00" in normalized_name
        or len(normalized_name) > 255
    ):
        raise DocmindHierarchyError("DOCMIND_FOLDER_NAME_INVALID")
    exists, parent = FileService.get_by_id(parent_file_id)
    if not exists or parent is None or parent.type != FileType.FOLDER.value:
        raise DocmindHierarchyError("DOCMIND_FOLDER_PARENT_INVALID")
    current = parent
    seen = set()
    while current.id != root.id:
        if current.id in seen:
            raise DocmindHierarchyError("DOCMIND_FOLDER_TREE_INVALID")
        seen.add(current.id)
        exists, current = FileService.get_by_id(current.parent_id)
        if not exists or current is None:
            raise DocmindHierarchyError("DOCMIND_FOLDER_OUTSIDE_PROJECT")
    folder = _ensure_folder(parent, normalized_name, tenant_id)
    _semantic_folder(context.project, folder, "", folder.name)
    return {"folder_id": folder.id, "name": folder.name, "parent_file_id": parent.id}


def _require_folder_in_source_root(context: Any, file_id: str) -> tuple[File, File]:
    root = _source_root(context)
    exists, folder = FileService.get_by_id(file_id)
    if (
        not exists
        or folder is None
        or folder.type != FileType.FOLDER.value
        or folder.tenant_id != context.project.tenant_id
    ):
        raise DocmindHierarchyError("DOCMIND_FOLDER_NOT_FOUND")
    current = folder
    seen: set[str] = set()
    while current.id != root.id:
        if current.id in seen:
            raise DocmindHierarchyError("DOCMIND_FOLDER_TREE_INVALID")
        seen.add(current.id)
        exists, current = FileService.get_by_id(current.parent_id)
        if not exists or current is None:
            raise DocmindHierarchyError("DOCMIND_FOLDER_OUTSIDE_PROJECT")
    return root, folder


def _descendant_folders(folder: File) -> list[File]:
    result = [folder]
    for child in _children(folder.id):
        if child.type == FileType.FOLDER.value:
            result.extend(_descendant_folders(child))
    return result


def _assert_folder_tree_mutable(context: Any, folder: File, operation: str) -> None:
    descendant_ids = [row.id for row in _descendant_folders(folder)]
    protected = (
        DocmindFolderVersion.select(DocmindFolderVersion.id)
        .join(
            DocmindCatalogVersion,
            on=(DocmindFolderVersion.version_id == DocmindCatalogVersion.id),
        )
        .where(
            (DocmindFolderVersion.source_file_id.in_(descendant_ids))
            & (
                DocmindCatalogVersion.lifecycle_state.in_(
                    ("READY", "PUBLISHED", "SUPERSEDED")
                )
            )
        )
        .exists()
    )
    if protected:
        raise DocmindHierarchyError("DOCMIND_FOLDER_PHYSICALLY_PROTECTED")
    document_ids: list[str] = []
    for current in _descendant_folders(folder):
        for child in _children(current.id):
            if child.type != FileType.FOLDER.value:
                document_ids.extend(
                    link.document_id
                    for link in File2DocumentService.get_by_file_id(child.id)
                )
    try:
        DocumentService.assert_documents_docmind_evidence_mutable(
            document_ids,
            operation,
            actor_id=context.project.tenant_id,
        )
    except DocmindProtectedEvidenceError as error:
        raise DocmindHierarchyError(error.code) from error


def update_folder(
    tenant_id: str,
    folder_id: str,
    *,
    parent_file_id: str | None,
    name: str | None,
) -> dict[str, Any]:
    context = _context(tenant_id)
    root, folder = _require_folder_in_source_root(context, folder_id)
    if folder.id == root.id:
        raise DocmindHierarchyError("DOCMIND_SOURCE_ROOT_IMMUTABLE")
    _assert_folder_tree_mutable(context, folder, "MOVE_OR_RENAME_FILE_TREE")
    updates: dict[str, Any] = {}
    if name is not None:
        normalized_name = unicodedata.normalize("NFC", name).strip()
        if (
            not normalized_name
            or normalized_name in {".", ".."}
            or "/" in normalized_name
            or "\\" in normalized_name
            or "\x00" in normalized_name
            or len(normalized_name) > 255
        ):
            raise DocmindHierarchyError("DOCMIND_FOLDER_NAME_INVALID")
        updates["name"] = normalized_name
    if parent_file_id is not None:
        _root, parent = _require_folder_in_source_root(context, parent_file_id)
        if parent.id in {row.id for row in _descendant_folders(folder)}:
            raise DocmindHierarchyError("DOCMIND_FOLDER_MOVE_CYCLE")
        updates["parent_id"] = parent.id
    if not updates:
        raise DocmindHierarchyError("DOCMIND_FOLDER_UPDATE_EMPTY")
    target_parent_id = updates.get("parent_id", folder.parent_id)
    target_name = updates.get("name", folder.name)
    collision = next(
        (
            child
            for child in _children(target_parent_id)
            if child.id != folder.id
            and _name_key(child.name) == _name_key(target_name)
        ),
        None,
    )
    if collision:
        raise DocmindHierarchyError("DOCMIND_IMPORT_PATH_CONFLICT")
    File.update(**updates, **_updates()).where(File.id == folder.id).execute()
    semantic = DocmindFolder.get_or_none(
        (DocmindFolder.project_id == context.project.id)
        & (DocmindFolder.source_file_id == folder.id)
    )
    if semantic is not None and "name" in updates:
        DocmindFolder.update(
            display_name=target_name,
            **_updates(),
        ).where(DocmindFolder.id == semantic.id).execute()
    return {
        "folder_id": folder.id,
        "name": target_name,
        "parent_file_id": target_parent_id,
    }


def delete_folder(tenant_id: str, folder_id: str) -> dict[str, Any]:
    context = _context(tenant_id)
    root, folder = _require_folder_in_source_root(context, folder_id)
    if folder.id == root.id:
        raise DocmindHierarchyError("DOCMIND_SOURCE_ROOT_IMMUTABLE")
    _assert_folder_tree_mutable(context, folder, "DELETE_FILE_TREE")
    if _children(folder.id):
        raise DocmindHierarchyError("DOCMIND_FOLDER_DELETE_NONEMPTY")
    FileService.delete_by_id(folder.id)
    return {"folder_id": folder.id, "deleted": True}


async def retry_import(
    tenant_id: str,
    job_id: str,
    raw_paths: list[str],
    file_objects: list[Any],
    idempotency_key: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    job = DocmindFolderImportJob.get_or_none(
        (DocmindFolderImportJob.id == job_id)
        & (DocmindFolderImportJob.project_id == context.project.id)
    )
    if job is None:
        raise DocmindHierarchyError("DOCMIND_IMPORT_JOB_NOT_FOUND")
    failed_paths = {
        item.relative_path
        for item in DocmindFolderImportItem.select().where(
            (DocmindFolderImportItem.job_id == job.id)
            & (DocmindFolderImportItem.lifecycle_state == "FAILED")
        )
    }
    normalized_paths = [normalize_relative_path(path) for path in raw_paths]
    if not failed_paths or not normalized_paths or not set(normalized_paths) <= failed_paths:
        raise DocmindHierarchyError("DOCMIND_IMPORT_RETRY_SCOPE_INVALID")
    return await import_local_folder(
        tenant_id,
        normalized_paths,
        file_objects,
        idempotency_key,
    )


def capture_hierarchy_draft(
    tenant_id: str,
    expected_active_version_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    context = _context(tenant_id)
    expected_parent_version_id = expected_active_version_id or None
    if context.project.active_version_id != expected_parent_version_id:
        raise DocmindHierarchyError("DOCMIND_ACTIVE_VERSION_CONFLICT")
    root = _source_root(context)
    tree = _tree_rows(root, context.project.dataset_id)
    folder_nodes = [row for row in tree if row["type"] == "folder"]
    semantic_by_file: dict[str, DocmindFolder] = {}
    for row in folder_nodes:
        exists, source_folder = FileService.get_by_id(row["file_id"])
        if not exists or source_folder is None:
            raise DocmindHierarchyError("DOCMIND_SOURCE_TREE_CHANGED")
        semantic_by_file[row["file_id"]] = _semantic_folder(
            context.project,
            source_folder,
            row["relative_path"],
            row["name"],
        )
    source_tree_projection = [
        {
            "file_id": row["file_id"],
            "parent_file_id": row["parent_file_id"],
            "relative_path": row["relative_path"],
            "type": row["type"],
            "document_id": row.get("document_id"),
        }
        for row in tree
    ]
    source_tree_hash = _hash(_json(source_tree_projection))
    eligible_registrations: list[
        tuple[DocmindRegistration | None, Document, File, DocmindFolder]
    ] = []
    registrations = list(
        DocmindRegistration.select()
        .join(
            DocmindRegistrationCurrent,
            on=(DocmindRegistration.id == DocmindRegistrationCurrent.registration_id),
        )
        .where(
            (DocmindRegistration.project_id == context.project.id)
            & (DocmindRegistration.lifecycle_state == "INDEXED")
        )
    )
    for registration in registrations:
        document = Document.get_or_none(Document.id == registration.document_id)
        file_row = File.get_or_none(File.id == registration.file_id)
        if document is None or file_row is None:
            continue
        links = File2DocumentService.get_by_file_id(file_row.id)
        if len(links) != 1 or links[0].document_id != document.id:
            continue
        blocker = docmind_registration_service.index_completion_blocker(
            context,
            document,
            captured_content_hash=registration.captured_content_hash,
        )
        parent_semantic = semantic_by_file.get(file_row.parent_id)
        if blocker or parent_semantic is None:
            continue
        eligible_registrations.append(
            (registration, document, file_row, parent_semantic)
        )
    eligible_document_ids = {
        document.id for _registration, document, _file, _folder in eligible_registrations
    }
    active_memberships = (
        list(
            DocmindFolderVersionDocument.select().where(
                DocmindFolderVersionDocument.version_id == expected_parent_version_id
            )
        )
        if expected_parent_version_id
        else []
    )
    for membership in active_memberships:
        if membership.document_id in eligible_document_ids:
            continue
        document = Document.get_or_none(Document.id == membership.document_id)
        links = File2DocumentService.get_by_document_id(membership.document_id)
        if document is None or len(links) != 1:
            raise DocmindHierarchyError(
                "DOCMIND_ACTIVE_DOCUMENT_OUTSIDE_SOURCE_ROOT"
            )
        file_row = File.get_or_none(File.id == links[0].file_id)
        parent_semantic = (
            semantic_by_file.get(file_row.parent_id) if file_row is not None else None
        )
        blocker = (
            docmind_registration_service.index_completion_blocker(
                context,
                document,
                captured_content_hash=document.content_hash,
            )
            if document is not None
            else "DOCMIND_DOCUMENT_MISSING"
        )
        if file_row is None or parent_semantic is None or blocker:
            raise DocmindHierarchyError(
                "DOCMIND_ACTIVE_DOCUMENT_OUTSIDE_SOURCE_ROOT"
            )
        eligible_registrations.append((None, document, file_row, parent_semantic))
        eligible_document_ids.add(document.id)
    if not eligible_registrations:
        raise DocmindHierarchyError("DOCMIND_HIERARCHY_NO_INDEXED_DOCUMENTS")
    payload = {
        "expected_active_version_id": expected_active_version_id,
        "source_tree_hash": source_tree_hash,
    }
    operation, replay = docmind_draft_service._idempotency_start(
        context,
        tenant_id,
        "CAPTURE_HIERARCHY_DRAFT",
        idempotency_key,
        payload,
    )
    if replay is not None:
        return replay

    version_id = get_uuid()
    version = DocmindCatalogVersion.create(
        id=version_id,
        project_id=context.project.id,
        parent_version_id=expected_parent_version_id,
        version_label=f"DRAFT-{version_id[:8]}",
        lifecycle_state="DRAFT",
        health_state="UNVALIDATED",
        health_reason=None,
        snapshot_hash=_hash("{}"),
        snapshot_json="{}",
        root_uri=f"viking://resources/docmind-catalog-{version_id}/",
        root_version=None,
        routing_card_set_hash=None,
        validation_report_hash=None,
        validated_at=None,
        validation_expires_at=None,
        readiness_mode="ADMIN_SAVED",
        source_ready_version_id=expected_parent_version_id,
        manual_saved_by=tenant_id,
        manual_saved_at=datetime.now(),
        snapshot_schema_version=2,
        source_tree_hash=source_tree_hash,
        root_identity_sha256=None,
        created_by=tenant_id,
        **_timestamps(),
    )
    active_cards = (
        {
            row.folder_id: row
            for row in DocmindFolderVersion.select().where(
                DocmindFolderVersion.version_id == expected_parent_version_id
            )
        }
        if expected_parent_version_id
        else {}
    )
    children_by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for row in folder_nodes:
        children_by_parent.setdefault(row["parent_file_id"], []).append(row)
    for siblings in children_by_parent.values():
        siblings.sort(key=lambda row: (row["name"].casefold(), row["file_id"]))
    for row in sorted(folder_nodes, key=lambda item: (item["depth"], item["relative_path"])):
        semantic = semantic_by_file[row["file_id"]]
        parent_semantic = semantic_by_file.get(row["parent_file_id"])
        siblings = children_by_parent.get(row["parent_file_id"], [])
        ordinal = next(index for index, item in enumerate(siblings) if item["file_id"] == row["file_id"])
        previous = active_cards.get(semantic.id)
        DocmindFolderVersion.create(
            id=get_uuid(),
            version_id=version.id,
            folder_id=semantic.id,
            l0_text=previous.l0_text if previous else None,
            l1_text=previous.l1_text if previous else None,
            l0_hash=previous.l0_hash if previous else None,
            l1_hash=previous.l1_hash if previous else None,
            generator_metadata=previous.generator_metadata if previous else {},
            parent_folder_id=parent_semantic.id if parent_semantic else None,
            source_file_id=row["file_id"],
            relative_path=(
                root.name
                if not row["relative_path"]
                else f"{root.name}/{row['relative_path']}"
            ),
            display_name=row["name"],
            ordinal=ordinal,
            depth=row["depth"],
            **_timestamps(),
        )

    membership_count_by_folder: dict[str, int] = {}
    for _registration, document, _file_row, parent_semantic in eligible_registrations:
        ordinal = membership_count_by_folder.get(parent_semantic.id, 0)
        membership_count_by_folder[parent_semantic.id] = ordinal + 1
        DocmindFolderVersionDocument.create(
            id=get_uuid(),
            version_id=version.id,
            folder_id=parent_semantic.id,
            document_id=document.id,
            ordinal=ordinal,
            captured_content_hash=document.content_hash,
            routing_digest_id=None,
            routing_digest_hash=None,
            chunk_set_fingerprint=None,
            **_timestamps(),
        )
    parent_memberships = {
        row.document_id: row
        for row in DocmindFolderVersionDocument.select().where(
            DocmindFolderVersionDocument.version_id == expected_active_version_id
        )
    }
    for ordinal, row in enumerate(
        DocmindFolderVersionDocument.select().where(
            DocmindFolderVersionDocument.version_id == version.id
        )
    ):
        parent = parent_memberships.get(row.document_id)
        if parent is None or parent.folder_id != row.folder_id:
            DocmindDraftChange.create(
                id=get_uuid(),
                draft_version_id=version.id,
                operation="ADD" if parent is None else "MOVE",
                document_id=row.document_id,
                registration_id=(
                    DocmindRegistrationCurrent.get(
                        (DocmindRegistrationCurrent.project_id == context.project.id)
                        & (DocmindRegistrationCurrent.document_id == row.document_id)
                    ).registration_id
                    if parent is None
                    else None
                ),
                from_folder_id=parent.folder_id if parent else None,
                to_folder_id=row.folder_id,
                expected_parent_folder_id=parent.folder_id if parent else None,
                expected_parent_membership_hash=(
                    docmind_draft_service._membership_hash(parent) if parent else None
                ),
                actor_id=tenant_id,
                ordinal=ordinal,
                **_timestamps(),
            )
    if current_source_tree_hash(tenant_id) != source_tree_hash:
        DocmindCatalogVersion.update(
            lifecycle_state="FAILED",
            health_state="INVALID",
            health_reason="DOCMIND_SOURCE_TREE_CHANGED",
            **_updates(),
        ).where(DocmindCatalogVersion.id == version.id).execute()
        DocmindIdempotencyOperation.delete().where(
            DocmindIdempotencyOperation.id == operation.id
        ).execute()
        raise DocmindHierarchyError("DOCMIND_SOURCE_TREE_CHANGED")
    snapshot_json, snapshot_hash = docmind_draft_service._snapshot(version)
    DocmindCatalogVersion.update(
        snapshot_json=snapshot_json,
        snapshot_hash=snapshot_hash,
        **_updates(),
    ).where(DocmindCatalogVersion.id == version.id).execute()
    result = docmind_draft_service._draft_result(
        context,
        DocmindCatalogVersion.get_by_id(version.id),
    )
    docmind_draft_service._idempotency_complete(operation, result)
    return result
