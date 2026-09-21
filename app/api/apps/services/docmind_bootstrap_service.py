"""Idempotent bootstrap for the loginless DocMind shared workspace.

The bootstrap deliberately creates only a RAGFlow dataset and its DocMind
project binding. It does not create or publish a Catalog: the first Catalog is
captured from an indexed document and must follow the normal Draft and Publish
review flow.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from peewee import IntegrityError

from api.apps.services import docmind_shared_workspace_service
from api.db.db_models import DocmindAuditEvent, DocmindProject
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService
from common.time_utils import current_timestamp

_DATASET_NAME = "DocMind Canary"
_CATALOG_SOURCE_DATABASE = "database"


class DocmindBootstrapError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]


def _timestamps() -> dict[str, object]:
    now = datetime.now()
    stamp = current_timestamp()
    return {
        "create_time": stamp,
        "create_date": now,
        "update_time": stamp,
        "update_date": now,
    }


def _shared_workspace_actor(tenant_id: str):
    if not docmind_shared_workspace_service.enabled():
        raise DocmindBootstrapError("DOCMIND_SHARED_WORKSPACE_DISABLED")
    user = docmind_shared_workspace_service.resolve_user()
    if user.id != tenant_id:
        raise DocmindBootstrapError("DOCMIND_SHARED_WORKSPACE_ACTOR_REQUIRED")
    return user


def _project_for_tenant(tenant_id: str) -> DocmindProject | None:
    projects = list(
        DocmindProject.select().where(
            (DocmindProject.tenant_id == tenant_id)
            & (DocmindProject.catalog_source_mode == _CATALOG_SOURCE_DATABASE)
        )
    )
    if len(projects) > 1:
        raise DocmindBootstrapError("DOCMIND_PROJECT_SERVING_AMBIGUOUS")
    return projects[0] if projects else None


def _ensure_dataset(tenant_id: str, dataset_id: str):
    exists, dataset = KnowledgebaseService.get_by_id(dataset_id)
    if exists:
        if dataset is None or dataset.tenant_id != tenant_id:
            raise DocmindBootstrapError("DOCMIND_BOOTSTRAP_DATASET_CONFLICT")
        return dataset

    created, payload = KnowledgebaseService.create_with_name(
        name=_DATASET_NAME,
        tenant_id=tenant_id,
        parser_id="naive",
        permission="me",
    )
    if not created:
        raise DocmindBootstrapError("DOCMIND_BOOTSTRAP_DATASET_CREATE_FAILED")
    ok, tenant = TenantService.get_by_id(tenant_id)
    if not ok or tenant is None:
        raise DocmindBootstrapError("DOCMIND_BOOTSTRAP_TENANT_MISSING")
    payload["id"] = dataset_id
    payload["embd_id"] = tenant.embd_id
    if not KnowledgebaseService.save(**payload):
        exists, dataset = KnowledgebaseService.get_by_id(dataset_id)
        if exists and dataset is not None and dataset.tenant_id == tenant_id:
            return dataset
        raise DocmindBootstrapError("DOCMIND_BOOTSTRAP_DATASET_CREATE_FAILED")
    exists, dataset = KnowledgebaseService.get_by_id(dataset_id)
    if not exists or dataset is None:
        raise DocmindBootstrapError("DOCMIND_BOOTSTRAP_DATASET_CREATE_FAILED")
    return dataset


def _public_result(project: DocmindProject, created: bool) -> dict[str, object]:
    return {
        "project_id": project.id,
        "dataset_id": project.dataset_id,
        "active_catalog_version_id": project.active_version_id,
        "created": created,
        "catalog_initialized": bool(project.active_version_id),
    }


def workspace_status(tenant_id: str) -> dict[str, object] | None:
    """Read bootstrap state without creating a Dataset or a project."""

    _shared_workspace_actor(tenant_id)
    project = _project_for_tenant(tenant_id)
    return _public_result(project, created=False) if project is not None else None


def bootstrap_shared_workspace(tenant_id: str) -> dict[str, object]:
    """Ensure the shared workspace has one empty, database-primary project.

    Stable IDs make retries idempotent. The unique tenant/dataset index is the
    cross-process final guard; a concurrent creator reads the winning project
    rather than provisioning a second workspace.
    """

    _shared_workspace_actor(tenant_id)
    existing = _project_for_tenant(tenant_id)
    if existing is not None:
        _ensure_dataset(tenant_id, existing.dataset_id)
        return _public_result(existing, created=False)

    dataset_id = _stable_id("docmind-canary-dataset", tenant_id)
    project_id = _stable_id("docmind-canary-project", tenant_id)
    _ensure_dataset(tenant_id, dataset_id)

    database = DocmindProject._meta.database
    try:
        with database.atomic():
            project = _project_for_tenant(tenant_id)
            if project is None:
                project = DocmindProject.create(
                    id=project_id,
                    tenant_id=tenant_id,
                    dataset_id=dataset_id,
                    active_version_id=None,
                    source_root_file_id=None,
                    catalog_source_mode=_CATALOG_SOURCE_DATABASE,
                    lock_version=0,
                    **_timestamps(),
                )
                DocmindAuditEvent.create(
                    id=_stable_id(
                        "docmind-audit", project_id, "WORKSPACE_BOOTSTRAPPED"
                    ),
                    project_id=project.id,
                    actor_id=tenant_id,
                    action="WORKSPACE_BOOTSTRAPPED",
                    target_type="PROJECT",
                    target_id=project.id,
                    before_version_id=None,
                    after_version_id=None,
                    outcome="SUCCESS",
                    trace_id=None,
                    details={"dataset_id": dataset_id, "catalog_initialized": False},
                    **_timestamps(),
                )
                return _public_result(project, created=True)
    except IntegrityError:
        pass

    project = _project_for_tenant(tenant_id)
    if project is None:
        raise DocmindBootstrapError("DOCMIND_BOOTSTRAP_PROJECT_CREATE_FAILED")
