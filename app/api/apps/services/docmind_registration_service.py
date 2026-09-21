import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from peewee import IntegrityError

from api.apps.services import docmind_api_service, docmind_canary_policy
from api.apps.services.document_api_service import reset_document_for_reparse
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindFolder,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindProject,
    DocmindRegistration,
    DocmindRegistrationCurrent,
    ParserRun,
    Task,
)
from api.db.services.document_service import DocmindProtectedEvidenceError, DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import TaskService
from common import settings
from common.constants import StatusEnum, TaskStatus
from common.doc_store.doc_store_base import OrderByExpr
from common.misc_utils import get_uuid, thread_pool_exec
from common.time_utils import current_timestamp
from rag.nlp import search

logger = logging.getLogger(__name__)


MAX_FILES_PER_REQUEST = 5
UPLOAD_VISIBILITY_GRACE_SECONDS = 300
INDEX_FINALIZATION_GRACE_SECONDS = 300
REGISTRATION_STATES = (
    "UPLOADING",
    "UPLOADED",
    "INDEX_QUEUED",
    "INDEXING",
    "INDEXED",
    "FAILED",
    "CANCELLED",
)
STATE_ORDER = {
    "UPLOADING": 0,
    "UPLOADED": 1,
    "INDEX_QUEUED": 2,
    "INDEXING": 3,
    "INDEXED": 4,
}
TERMINAL_STATES = frozenset({"INDEXED", "FAILED", "CANCELLED"})
ALLOWED_TRANSITIONS = {
    "UPLOADING": frozenset({"UPLOADED", "FAILED", "CANCELLED"}),
    "UPLOADED": frozenset({"INDEX_QUEUED", "FAILED", "CANCELLED"}),
    "INDEX_QUEUED": frozenset({"INDEXING", "FAILED", "CANCELLED"}),
    "INDEXING": frozenset({"INDEXED", "FAILED", "CANCELLED"}),
}


class DocmindRegistrationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class OwnerContext:
    project: DocmindProject
    catalog: docmind_api_service.Catalog


class _IdentifiedFile:
    def __init__(self, document_id: str, file_object: Any):
        self.id = document_id
        self.filename = file_object.filename
        self._file_object = file_object
        self.fingerprint = getattr(file_object, "fingerprint", None)

    def read(self) -> bytes:
        return self._file_object.read()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(*parts: str) -> str:
    return _hash("\x1f".join(parts))[:32]


def _timestamps() -> dict[str, Any]:
    now = datetime.now()
    stamp = current_timestamp()
    return {
        "create_time": stamp,
        "create_date": now,
        "update_time": stamp,
        "update_date": now,
    }


def _update_timestamps() -> dict[str, Any]:
    return {"update_time": current_timestamp(), "update_date": datetime.now()}


def _owner_context(tenant_id: str) -> OwnerContext:
    try:
        catalog = docmind_api_service._load_catalog()
    except docmind_api_service.DocmindCatalogNotInitializedError:
        return _bootstrap_owner_context(tenant_id)
    project = DocmindProject.get_or_none(
        (DocmindProject.tenant_id == tenant_id)
        & (DocmindProject.dataset_id == catalog.dataset_id)
    )
    if project is None:
        raise DocmindRegistrationError("DOCMIND_PROJECT_NOT_FOUND")
    if not KnowledgebaseService.query(id=project.dataset_id, tenant_id=tenant_id):
        raise DocmindRegistrationError("DOCMIND_DATASET_OWNER_REQUIRED")
    return OwnerContext(project=project, catalog=catalog)


def _bootstrap_owner_context(tenant_id: str) -> OwnerContext:
    projects = list(
        DocmindProject.select().where(
            (DocmindProject.tenant_id == tenant_id)
            & (DocmindProject.catalog_source_mode == "database")
            & DocmindProject.active_version_id.is_null(True)
        )
    )
    if len(projects) != 1:
        raise DocmindRegistrationError("DOCMIND_PROJECT_NOT_FOUND")
    project = projects[0]
    if not KnowledgebaseService.query(id=project.dataset_id, tenant_id=tenant_id):
        raise DocmindRegistrationError("DOCMIND_DATASET_OWNER_REQUIRED")
    catalog = docmind_api_service.Catalog(
        dataset_id=project.dataset_id,
        root_uri="",
        folders={},
        source="database",
        version_id=None,
    )
    return OwnerContext(project=project, catalog=catalog)


def can_administer(tenant_id: str) -> bool:
    try:
        _owner_context(tenant_id)
        return True
    except DocmindRegistrationError:
        return False
    except Exception:
        logger.warning("DocMind administrator capability check unavailable")
        return False


def _folder(context: OwnerContext, folder_slug: str) -> DocmindFolder:
    active_folder = folder_slug in context.catalog.folders
    identity_expression = (
        DocmindFolder.id == folder_slug
        if getattr(context.catalog, "folder_tree", ()) or not active_folder
        else DocmindFolder.slug == folder_slug
    )
    folder = DocmindFolder.get_or_none(
        (DocmindFolder.project_id == context.project.id)
        & identity_expression
        & (DocmindFolder.enabled == True)
    )
    if folder is None:
        raise DocmindRegistrationError("DOCMIND_FOLDER_INVALID")
    if not active_folder:
        project = DocmindProject.get_by_id(context.project.id)
        if not project.source_root_file_id or not folder.source_file_id:
            raise DocmindRegistrationError("DOCMIND_FOLDER_INVALID")
        exists, current = FileService.get_by_id(folder.source_file_id)
        seen: set[str] = set()
        while exists and current is not None and current.id != project.source_root_file_id:
            if current.id in seen:
                raise DocmindRegistrationError("DOCMIND_FOLDER_INVALID")
            seen.add(current.id)
            exists, current = FileService.get_by_id(current.parent_id)
        if not exists or current is None or current.id != project.source_root_file_id:
            raise DocmindRegistrationError("DOCMIND_FOLDER_INVALID")
    return folder


def _audit(
    project_id: str,
    actor_id: str,
    action: str,
    registration_id: str,
    outcome: str,
    details: dict[str, Any],
) -> None:
    DocmindAuditEvent.create(
        id=get_uuid(),
        project_id=project_id,
        actor_id=actor_id,
        action=action,
        target_type="REGISTRATION",
        target_id=registration_id,
        before_version_id=None,
        after_version_id=None,
        outcome=outcome,
        trace_id=None,
        details=details,
        **_timestamps(),
    )


def _transition(
    registration: DocmindRegistration,
    target_state: str,
    *,
    actor_id: str,
    error_code: str | None = None,
    error_message: str | None = None,
    updates: dict[str, Any] | None = None,
) -> DocmindRegistration:
    source_state = registration.lifecycle_state
    if target_state == source_state:
        return registration
    if target_state not in ALLOWED_TRANSITIONS.get(source_state, frozenset()):
        raise DocmindRegistrationError("DOCMIND_REGISTRATION_TRANSITION_INVALID")
    values = {
        "lifecycle_state": target_state,
        "error_code": error_code,
        "error_message": error_message,
        **(updates or {}),
        **_update_timestamps(),
    }
    database = DocmindRegistration._meta.database
    with database.atomic():
        changed = (
            DocmindRegistration.update(**values)
            .where(
                (DocmindRegistration.id == registration.id)
                & (DocmindRegistration.lifecycle_state == source_state)
            )
            .execute()
        )
        if changed != 1:
            current = DocmindRegistration.get_by_id(registration.id)
            if current.lifecycle_state == target_state:
                return current
            if (
                source_state in STATE_ORDER
                and target_state in STATE_ORDER
                and current.lifecycle_state in STATE_ORDER
                and STATE_ORDER[current.lifecycle_state] >= STATE_ORDER[target_state]
            ):
                return current
            raise DocmindRegistrationError("DOCMIND_REGISTRATION_CONFLICT")
        _audit(
            registration.project_id,
            actor_id,
            "REGISTRATION_STATE_CHANGED",
            registration.id,
            "SUCCESS",
            {
                "from_state": source_state,
                "to_state": target_state,
                "error_code": error_code,
            },
        )
    return DocmindRegistration.get_by_id(registration.id)


def _create_attempt(
    context: OwnerContext,
    folder: DocmindFolder,
    document_id: str,
    actor_id: str,
    *,
    retry_of_id: str | None = None,
    file_id: str | None = None,
    captured_content_hash: str | None = None,
    expected_current_registration_id: str | None = None,
) -> DocmindRegistration:
    registration_id = get_uuid()
    pointer_id = _stable_id("docmind-registration-current", context.project.id, document_id)
    database = DocmindRegistration._meta.database
    with database.atomic():
        registration = DocmindRegistration.create(
            id=registration_id,
            project_id=context.project.id,
            folder_id=folder.id,
            document_id=document_id,
            file_id=file_id,
            captured_content_hash=captured_content_hash,
            lifecycle_state="UPLOADING",
            error_code=None,
            error_message=None,
            created_by=actor_id,
            retry_of_id=retry_of_id,
            **_timestamps(),
        )
        current = DocmindRegistrationCurrent.get_or_none(
            (DocmindRegistrationCurrent.project_id == context.project.id)
            & (DocmindRegistrationCurrent.document_id == document_id)
        )
        if current is None:
            if expected_current_registration_id is not None:
                raise DocmindRegistrationError("DOCMIND_REGISTRATION_CURRENT_MISSING")
            DocmindRegistrationCurrent.create(
                id=pointer_id,
                project_id=context.project.id,
                document_id=document_id,
                registration_id=registration_id,
                lock_version=0,
                **_timestamps(),
            )
        else:
            if current.registration_id != expected_current_registration_id:
                raise DocmindRegistrationError("DOCMIND_REGISTRATION_CURRENT_CONFLICT")
            changed = (
                DocmindRegistrationCurrent.update(
                    registration_id=registration_id,
                    lock_version=current.lock_version + 1,
                    **_update_timestamps(),
                )
                .where(
                    (DocmindRegistrationCurrent.id == current.id)
                    & (DocmindRegistrationCurrent.registration_id == expected_current_registration_id)
                    & (DocmindRegistrationCurrent.lock_version == current.lock_version)
                )
                .execute()
            )
            if changed != 1:
                raise DocmindRegistrationError("DOCMIND_REGISTRATION_CURRENT_CONFLICT")
        _audit(
            context.project.id,
            actor_id,
            "REGISTRATION_CREATED",
            registration_id,
            "SUCCESS",
            {"state": "UPLOADING", "retry": retry_of_id is not None},
        )
    return registration


def _file_id(document_id: str) -> str | None:
    relations = File2DocumentService.get_by_document_id(document_id)
    return relations[0].file_id if relations else None


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, DocmindProtectedEvidenceError):
        return error.code
    if isinstance(error, DocmindRegistrationError):
        return error.code
    return "DOCMIND_REGISTRATION_EXECUTION_FAILED"


def _validate_canary_registration_files(file_objects: list[Any]) -> None:
    if not docmind_canary_policy.enabled():
        return
    if len(file_objects) != 1:
        raise DocmindRegistrationError("DOCMIND_CANARY_EXACTLY_ONE_DOCUMENT")
    file_object = file_objects[0]
    stream = getattr(file_object, "stream", None)
    if stream is None or not hasattr(stream, "seek"):
        raise DocmindRegistrationError("DOCMIND_CANARY_PDF_INVALID")
    try:
        position = stream.tell()
        blob = stream.read()
        stream.seek(position)
    except Exception as error:
        raise DocmindRegistrationError("DOCMIND_CANARY_PDF_INVALID") from error
    if not isinstance(blob, bytes):
        raise DocmindRegistrationError("DOCMIND_CANARY_PDF_INVALID")
    try:
        docmind_canary_policy.validate_single_pdf(file_object.filename, blob)
    except docmind_canary_policy.DocmindCanaryPolicyError as error:
        raise DocmindRegistrationError(error.code) from error


async def register_documents(tenant_id: str, folder_slug: str, file_objects: list[Any]) -> dict[str, Any]:
    context = _owner_context(tenant_id)
    folder = _folder(context, folder_slug)
    if not 1 <= len(file_objects) <= MAX_FILES_PER_REQUEST:
        raise DocmindRegistrationError("DOCMIND_REGISTRATION_FILE_COUNT_INVALID")
    if any(file_object is None or not getattr(file_object, "filename", "") for file_object in file_objects):
        raise DocmindRegistrationError("DOCMIND_REGISTRATION_FILE_INVALID")

    _validate_canary_registration_files(file_objects)

    results: list[dict[str, Any]] = []
    for file_object in file_objects:
        document_id = get_uuid()
        registration = _create_attempt(context, folder, document_id, tenant_id)
        try:
            from pathlib import Path

            from rag.parser_platform.config import ParserPlatformConfig

            suffix = Path(file_object.filename).suffix.lower()
            hwp_config = ParserPlatformConfig.from_env()
            if suffix in {".hwp", ".hwpx"} and hwp_config.hwp_registration_mode == "canary":
                registration = _transition(
                    registration,
                    "FAILED",
                    actor_id=tenant_id,
                    error_code="PARSER_PLATFORM_HWP_CANARY_BOOTSTRAP_REQUIRED",
                    error_message=(
                        "먼저 RAGFlow upload-only로 문서를 등록해 ID를 확인한 뒤 canary 설정을 적용하고 "
                        "수동 분석을 시작하세요."
                    ),
                )
                results.append(
                    _public_registration(
                        registration,
                        dataset_id=context.project.dataset_id,
                        folder_slug=folder.slug,
                    )
                )
                continue
            identified_file = _IdentifiedFile(document_id, file_object)
            errors, uploaded = await thread_pool_exec(
                FileService.upload_document,
                KnowledgebaseService.get_by_id(context.project.dataset_id)[1],
                [identified_file],
                tenant_id,
            )
            if errors or len(uploaded) != 1:
                registration = _transition(
                    registration,
                    "FAILED",
                    actor_id=tenant_id,
                    error_code="DOCMIND_UPLOAD_FAILED",
                    error_message="Document upload failed",
                )
            else:
                document = uploaded[0][0]
                registration = _transition(
                    registration,
                    "UPLOADED",
                    actor_id=tenant_id,
                    updates={
                        "file_id": _file_id(document_id),
                        "captured_content_hash": document.get("content_hash"),
                    },
                )
                await thread_pool_exec(DocumentService.run, tenant_id, document, {})
                registration = _transition(
                    registration,
                    "INDEX_QUEUED",
                    actor_id=tenant_id,
                )
        except Exception as error:
            if registration.lifecycle_state not in TERMINAL_STATES:
                registration = _transition(
                    registration,
                    "FAILED",
                    actor_id=tenant_id,
                    error_code=_safe_error_code(error),
                    error_message="Registration failed",
                )
        results.append(
            _public_registration(
                registration,
                dataset_id=context.project.dataset_id,
                folder_slug=folder.slug,
            )
        )

    return {
        "project_id": context.project.id,
        "dataset_id": context.project.dataset_id,
        "catalog_version_id": context.project.active_version_id,
        "folder_id": folder.slug,
        "results": results,
    }


def _retrievable_chunk_exists(document: Any, tenant_id: str) -> bool:
    index_name = search.index_name(tenant_id)
    if not settings.docStoreConn.index_exist(index_name, document.kb_id):
        return False
    result = settings.docStoreConn.search(
        ["id"],
        [],
        {"doc_id": document.id, "must_not": {"exists": "compile_kwd"}},
        [],
        OrderByExpr(),
        0,
        1,
        index_name,
        [document.kb_id],
    )
    return int(settings.docStoreConn.get_total(result) or 0) > 0


def index_completion_blocker(
    context: OwnerContext,
    document: Any,
    *,
    captured_content_hash: str | None,
) -> str | None:
    if document is None:
        return "DOCMIND_DOCUMENT_MISSING"
    if document.kb_id != context.project.dataset_id:
        return "DOCMIND_DOCUMENT_DATASET_MISMATCH"
    if str(document.run) != TaskStatus.DONE.value:
        return "DOCMIND_DOCUMENT_NOT_DONE"
    if float(document.progress or 0) != 1.0:
        return "DOCMIND_DOCUMENT_PROGRESS_INCOMPLETE"
    if str(document.status) != StatusEnum.VALID.value:
        return "DOCMIND_DOCUMENT_STATUS_INVALID"
    if int(document.chunk_num or 0) <= 0:
        return "DOCMIND_DOCUMENT_CHUNKS_EMPTY"
    tasks = list(TaskService.query(doc_id=document.id))
    if any(0 <= float(task.progress or 0) < 1 for task in tasks):
        return "DOCMIND_DOCUMENT_TASK_UNFINISHED"
    if not document.content_hash:
        return "DOCMIND_DOCUMENT_CONTENT_HASH_MISSING"
    if captured_content_hash and document.content_hash != captured_content_hash:
        return "DOCMIND_DOCUMENT_CONTENT_HASH_CHANGED"
    if not _retrievable_chunk_exists(document, context.project.tenant_id):
        return "DOCMIND_DOCUMENT_INDEX_EMPTY"
    return None


def _refresh_registration(
    context: OwnerContext,
    registration: DocmindRegistration,
) -> tuple[DocmindRegistration, str | None]:
    if registration.lifecycle_state in TERMINAL_STATES:
        return registration, registration.error_code
    exists, document = DocumentService.get_by_id(registration.document_id)
    if not exists:
        if registration.lifecycle_state == "UPLOADING":
            elapsed = (
                (datetime.now() - registration.create_date).total_seconds()
                if isinstance(registration.create_date, datetime)
                else 0
            )
            if elapsed < UPLOAD_VISIBILITY_GRACE_SECONDS:
                return registration, "DOCMIND_UPLOAD_IN_PROGRESS"
        return (
            _transition(
                registration,
                "FAILED",
                actor_id=context.project.tenant_id,
                error_code="DOCMIND_DOCUMENT_MISSING",
                error_message="Document is unavailable",
            ),
            "DOCMIND_DOCUMENT_MISSING",
        )
    if registration.lifecycle_state == "UPLOADING":
        registration = _transition(
            registration,
            "UPLOADED",
            actor_id=context.project.tenant_id,
            updates={
                "file_id": _file_id(document.id),
                "captured_content_hash": document.content_hash,
            },
        )
    if str(document.run) == TaskStatus.CANCEL.value:
        return (
            _transition(
                registration,
                "CANCELLED",
                actor_id=context.project.tenant_id,
                error_code="DOCMIND_INDEX_CANCELLED",
                error_message="Indexing was cancelled",
            ),
            "DOCMIND_INDEX_CANCELLED",
        )
    if str(document.run) == TaskStatus.FAIL.value or float(document.progress or 0) < 0:
        return (
            _transition(
                registration,
                "FAILED",
                actor_id=context.project.tenant_id,
                error_code="DOCMIND_INDEX_FAILED",
                error_message="Indexing failed",
            ),
            "DOCMIND_INDEX_FAILED",
        )
    if registration.lifecycle_state == "UPLOADED":
        registration = _transition(
            registration,
            "INDEX_QUEUED",
            actor_id=context.project.tenant_id,
        )
    if str(document.run) != TaskStatus.DONE.value or float(document.progress or 0) != 1.0:
        if registration.lifecycle_state == "INDEX_QUEUED":
            registration = _transition(
                registration,
                "INDEXING",
                actor_id=context.project.tenant_id,
            )
        return registration, "DOCMIND_INDEX_IN_PROGRESS"

    blocker = index_completion_blocker(
        context,
        document,
        captured_content_hash=registration.captured_content_hash,
    )
    if blocker in {
        "DOCMIND_DOCUMENT_TASK_UNFINISHED",
        "DOCMIND_DOCUMENT_CHUNKS_EMPTY",
        "DOCMIND_DOCUMENT_INDEX_EMPTY",
    }:
        if registration.lifecycle_state == "INDEX_QUEUED":
            registration = _transition(
                registration,
                "INDEXING",
                actor_id=context.project.tenant_id,
            )
        document_updated_at = getattr(document, "update_date", None)
        elapsed = (
            (datetime.now() - document_updated_at).total_seconds()
            if isinstance(document_updated_at, datetime)
            else 0
        )
        if elapsed < INDEX_FINALIZATION_GRACE_SECONDS:
            return registration, "DOCMIND_INDEX_FINALIZING"
    if blocker:
        if registration.lifecycle_state == "INDEX_QUEUED":
            registration = _transition(
                registration,
                "INDEXING",
                actor_id=context.project.tenant_id,
            )
        return (
            _transition(
                registration,
                "FAILED",
                actor_id=context.project.tenant_id,
                error_code=blocker,
                error_message="Index completion gate failed",
            ),
            blocker,
        )
    if registration.lifecycle_state == "INDEX_QUEUED":
        registration = _transition(
            registration,
            "INDEXING",
            actor_id=context.project.tenant_id,
        )
    return (
        _transition(
            registration,
            "INDEXED",
            actor_id=context.project.tenant_id,
            updates={"captured_content_hash": document.content_hash},
        ),
        None,
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _public_registration(
    registration: DocmindRegistration,
    *,
    dataset_id: str,
    folder_slug: str | None = None,
    blocker_code: str | None = None,
    is_current: bool = True,
    active_catalog_member: bool = False,
) -> dict[str, Any]:
    if folder_slug is None:
        folder = DocmindFolder.get_or_none(DocmindFolder.id == registration.folder_id)
        folder_slug = folder.slug if folder else None
    exists, document = DocumentService.get_by_id(registration.document_id)
    document_exists = bool(
        exists
        and document is not None
        and document.kb_id == dataset_id
    )
    parser_run = None
    if document_exists:
        from rag.parser_platform.config import ParserPlatformConfig

        parser_config = ParserPlatformConfig.from_env()
        if (parser_config.enabled or parser_config.hwp_enabled) and ParserRun.table_exists():
            parser_run = (
                ParserRun.select()
                .where(ParserRun.doc_id == registration.document_id)
                .order_by(ParserRun.create_time.desc())
                .first()
            )
    parser_error_message = None
    if parser_run and parser_run.error_code:
        from rag.parser_platform.errors import ERRORS

        error_info = ERRORS.get(parser_run.error_code)
        parser_error_message = error_info.message_ko if error_info else "문서 분석 중 오류가 발생했습니다."
    return {
        "dataset_id": dataset_id,
        "registration_id": registration.id,
        "document_id": registration.document_id,
        "document_exists": document_exists,
        "document_name": document.name if document_exists else None,
        "folder_id": folder_slug,
        "state": registration.lifecycle_state,
        "error_code": registration.error_code,
        "error_message": registration.error_message,
        "blocker_code": blocker_code or registration.error_code,
        "progress": float(document.progress or 0) if document_exists else 0.0,
        "chunk_count": int(document.chunk_num or 0) if document_exists else 0,
        "index_ready": registration.lifecycle_state == "INDEXED",
        "draft_eligible": registration.lifecycle_state == "INDEXED",
        "active_catalog_member": active_catalog_member,
        "retry_allowed": (
            is_current
            and registration.lifecycle_state in {"FAILED", "CANCELLED"}
            and document_exists
        ),
        "is_current": is_current,
        "retry_of_id": registration.retry_of_id,
        "created_at": _iso(registration.create_date),
        "updated_at": _iso(registration.update_date),
        "parser_run": (
            {
                "parse_run_id": parser_run.id,
                "chunk_set_id": parser_run.chunk_set_id,
                "active_chunk_set_id": document.active_chunk_set_id,
                "active": document.active_chunk_set_id == parser_run.chunk_set_id,
                "source_format": parser_run.source_format,
                "selection_reason": f"file_format_{parser_run.source_format}",
                "parser_name": parser_run.parser_name,
                "parser_version": parser_run.parser_version,
                "model_version": parser_run.model_version,
                "backend": parser_run.backend,
                "schema_version": parser_run.schema_version,
                "phase": parser_run.lifecycle,
                "warnings": parser_run.warnings or [],
                "error_code": parser_run.error_code,
                "error_message": parser_error_message,
                "raw_artifact_ref": parser_run.raw_artifact_ref,
                "expected_pages": parser_run.expected_page_count,
                "completed_pages": parser_run.completed_page_count,
                "reused_pages": parser_run.reused_page_count,
                "failed_pages": parser_run.failed_page_count,
            }
            if parser_run
            else None
        ),
    }


def list_registrations(tenant_id: str, states: list[str] | None = None) -> dict[str, Any]:
    context = _owner_context(tenant_id)
    if states:
        if any(state not in REGISTRATION_STATES for state in states):
            raise DocmindRegistrationError("DOCMIND_REGISTRATION_STATE_INVALID")
    query = DocmindRegistration.select().where(DocmindRegistration.project_id == context.project.id)
    if states:
        query = query.where(DocmindRegistration.lifecycle_state.in_(states))
    registrations = list(query.order_by(DocmindRegistration.create_time.desc()).limit(100))
    current_ids = {
        row.registration_id
        for row in DocmindRegistrationCurrent.select().where(
            DocmindRegistrationCurrent.project_id == context.project.id
        )
    }
    active_catalog_document_ids = {
        row.document_id
        for row in DocmindFolderVersionDocument.select(
            DocmindFolderVersionDocument.document_id
        ).where(
            DocmindFolderVersionDocument.version_id
            == context.project.active_version_id
        )
    }
    results: list[dict[str, Any]] = []
    for registration in registrations:
        blocker = registration.error_code
        if registration.id in current_ids and registration.lifecycle_state not in TERMINAL_STATES:
            registration, blocker = _refresh_registration(context, registration)
        results.append(
            _public_registration(
                registration,
                dataset_id=context.project.dataset_id,
                blocker_code=blocker,
                is_current=registration.id in current_ids,
                active_catalog_member=(
                    registration.document_id in active_catalog_document_ids
                ),
            )
        )
    return {
        "project_id": context.project.id,
        "dataset_id": context.project.dataset_id,
        "catalog_version_id": context.project.active_version_id,
        "registrations": results,
    }


def _idempotency_start(
    context: OwnerContext,
    actor_id: str,
    key: str,
    registration_id: str,
) -> tuple[DocmindIdempotencyOperation, dict[str, Any] | None]:
    if not key or len(key) > 128:
        raise DocmindRegistrationError("DOCMIND_IDEMPOTENCY_KEY_REQUIRED")
    request_hash = _hash(json.dumps({"registration_id": registration_id}, sort_keys=True))
    predicate = (
        (DocmindIdempotencyOperation.project_id == context.project.id)
        & (DocmindIdempotencyOperation.actor_id == actor_id)
        & (DocmindIdempotencyOperation.operation == "RETRY_REGISTRATION")
        & (DocmindIdempotencyOperation.idempotency_key == key)
    )

    def replay(existing: DocmindIdempotencyOperation):
        if existing.request_hash != request_hash:
            raise DocmindRegistrationError("DOCMIND_IDEMPOTENCY_KEY_PAYLOAD_CONFLICT")
        if existing.state == "COMPLETE" and existing.result_json:
            return existing, json.loads(existing.result_json)
        if existing.state == "FAILED" and existing.result_json:
            stored = json.loads(existing.result_json)
            raise DocmindRegistrationError(
                str(stored.get("error_code") or "DOCMIND_REGISTRATION_EXECUTION_FAILED")
            )
        raise DocmindRegistrationError("DOCMIND_IDEMPOTENCY_OPERATION_IN_PROGRESS")

    existing = DocmindIdempotencyOperation.get_or_none(predicate)
    if existing:
        return replay(existing)
    try:
        operation = DocmindIdempotencyOperation.create(
            id=get_uuid(),
            project_id=context.project.id,
            actor_id=actor_id,
            operation="RETRY_REGISTRATION",
            idempotency_key=key,
            request_hash=request_hash,
            state="STARTED",
            result_json=None,
            status_code=None,
            **_timestamps(),
        )
    except IntegrityError:
        existing = DocmindIdempotencyOperation.get_or_none(predicate)
        if existing is None:
            raise
        return replay(existing)
    return operation, None


async def retry_registration(
    tenant_id: str,
    registration_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    context = _owner_context(tenant_id)
    original = DocmindRegistration.get_or_none(
        (DocmindRegistration.id == registration_id)
        & (DocmindRegistration.project_id == context.project.id)
    )
    if original is None:
        raise DocmindRegistrationError("DOCMIND_REGISTRATION_NOT_FOUND")
    if original.lifecycle_state not in {"FAILED", "CANCELLED"}:
        raise DocmindRegistrationError("DOCMIND_REGISTRATION_RETRY_INVALID_STATE")
    exists, document = DocumentService.get_by_id(original.document_id)
    if not exists:
        raise DocmindRegistrationError("DOCMIND_REUPLOAD_REQUIRED")
    if document.kb_id != context.project.dataset_id:
        raise DocmindRegistrationError("DOCMIND_DOCUMENT_DATASET_MISMATCH")
    operation, replay = _idempotency_start(
        context,
        tenant_id,
        idempotency_key,
        registration_id,
    )
    if replay is not None:
        return {**replay, "dataset_id": context.project.dataset_id}
    try:
        DocumentService.assert_docmind_evidence_mutable(
            document.id,
            "RETRY_DOCMIND_REGISTRATION",
            actor_id=tenant_id,
        )
        folder = DocmindFolder.get_by_id(original.folder_id)
        registration = _create_attempt(
            context,
            folder,
            document.id,
            tenant_id,
            retry_of_id=original.id,
            file_id=original.file_id,
            captured_content_hash=document.content_hash,
            expected_current_registration_id=original.id,
        )
        registration = _transition(
            registration,
            "UPLOADED",
            actor_id=tenant_id,
        )
        blocker = index_completion_blocker(
            context,
            document,
            captured_content_hash=registration.captured_content_hash,
        )
        if blocker is None:
            registration = _transition(
                registration,
                "INDEX_QUEUED",
                actor_id=tenant_id,
            )
            registration, blocker = _refresh_registration(context, registration)
            if blocker is not None or registration.lifecycle_state != "INDEXED":
                raise DocmindRegistrationError(
                    blocker or "DOCMIND_REGISTRATION_EXECUTION_FAILED"
                )
            result = _public_registration(
                registration,
                dataset_id=context.project.dataset_id,
                folder_slug=folder.slug,
            )
            DocmindIdempotencyOperation.update(
                state="COMPLETE",
                result_json=json.dumps(result, ensure_ascii=False, sort_keys=True),
                status_code=0,
                **_update_timestamps(),
            ).where(DocmindIdempotencyOperation.id == operation.id).execute()
            return result
        reset_error = await thread_pool_exec(
            reset_document_for_reparse,
            document,
            tenant_id,
        )
        if reset_error is not None:
            raise DocmindRegistrationError("DOCMIND_RETRY_RESET_FAILED")
        TaskService.filter_delete([Task.doc_id == document.id])
        exists, refreshed_document = DocumentService.get_by_id(document.id)
        if not exists:
            raise DocmindRegistrationError("DOCMIND_DOCUMENT_MISSING")
        await thread_pool_exec(
            DocumentService.run,
            tenant_id,
            refreshed_document.to_dict(),
            {},
        )
        registration = _transition(
            registration,
            "INDEX_QUEUED",
            actor_id=tenant_id,
        )
        result = _public_registration(
            registration,
            dataset_id=context.project.dataset_id,
            folder_slug=folder.slug,
        )
        DocmindIdempotencyOperation.update(
            state="COMPLETE",
            result_json=json.dumps(result, ensure_ascii=False, sort_keys=True),
            status_code=0,
            **_update_timestamps(),
        ).where(DocmindIdempotencyOperation.id == operation.id).execute()
        return result
    except Exception as error:
        DocmindIdempotencyOperation.update(
            state="FAILED",
            result_json=json.dumps({"error_code": _safe_error_code(error)}, sort_keys=True),
            status_code=1,
            **_update_timestamps(),
        ).where(DocmindIdempotencyOperation.id == operation.id).execute()
        raise
