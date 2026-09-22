"""Seed one isolated logical cloud-source mapping for the Windows E2E harness.

This script never accepts or stores a physical source root, plaintext, a model
credential, or a worker secret.  The relative path is supplied only through the
container environment and is persisted as normal DocMind source metadata.
"""

from __future__ import annotations

import json
import os

from api.apps.services import docmind_bootstrap_service, docmind_ingestion_service
from api.apps.services.docmind_shared_workspace_service import resolve_user
from api.db.db_models import (
    DB,
    DocmindFolder,
    DocmindProject,
    DocmindSource,
    Document,
    Knowledgebase,
)
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService

SOURCE_ID = "dept-2-e2e"
DOCUMENT_ID = "cc43dd4fe134a052269fe9dbf8067589"
FOLDER_ID = "17e6a865915e2bf7e752b97eb15fd6d0"
LOCAL_EMBEDDING_MODEL = "BAAI/bge-m3@Builtin"


def fail(code: str) -> None:
    print(json.dumps({"ready": False, "error": code}, separators=(",", ":")))
    raise SystemExit(2)


def main() -> None:
    if os.environ.get("DOCMIND_E2E_BOOTSTRAP_ONLY") != "1":
        fail("DOCMIND_E2E_BOOTSTRAP_ONLY_REQUIRED")
    relative_path = os.environ.get("DOCMIND_E2E_RELATIVE_PATH", "").strip()
    if not relative_path or os.path.isabs(relative_path) or ".." in relative_path.replace("\\", "/").split("/"):
        fail("DOCMIND_E2E_RELATIVE_PATH_INVALID")

    with DB.connection_context():
        user = resolve_user()
        workspace = docmind_bootstrap_service.bootstrap_shared_workspace(user.id)
        project = DocmindProject.get_by_id(str(workspace["project_id"]))
        dataset = Knowledgebase.get_by_id(project.dataset_id)
        tenant_ok, tenant = TenantService.get_by_id(user.id)
        if not tenant_ok or tenant is None:
            fail("DOCMIND_E2E_TENANT_MISSING")
        tenant_embedding = str(tenant.embd_id or "").strip()
        if not tenant_embedding:
            tenant_embedding = LOCAL_EMBEDDING_MODEL
            TenantService.update_by_id(user.id, {"embd_id": tenant_embedding, "tenant_embd_id": None})
        if not str(dataset.embd_id or "").strip():
            KnowledgebaseService.update_by_id(
                dataset.id,
                {"embd_id": tenant_embedding, "tenant_embd_id": None},
            )
            dataset = Knowledgebase.get_by_id(project.dataset_id)

        folder = DocmindFolder.get_or_none(DocmindFolder.id == FOLDER_ID)
        if folder is None:
            folder = DocmindFolder.create(
                id=FOLDER_ID,
                project_id=project.id,
                slug="approved-word-e2e",
                display_name="Approved Word E2E",
                ordinal=0,
            )
        elif folder.project_id != project.id:
            fail("DOCMIND_E2E_FOLDER_CONFLICT")

        source = DocmindSource.get_or_none(DocmindSource.id == SOURCE_ID)
        if source is None:
            source = DocmindSource.create(
                id=SOURCE_ID,
                project_id=project.id,
                display_name="[DEPT_2 E2E]:",
                default_folder_id=folder.id,
            )
        elif source.project_id != project.id or source.default_folder_id != folder.id:
            fail("DOCMIND_E2E_SOURCE_CONFLICT")

        document = Document.get_or_none(Document.id == DOCUMENT_ID)
        if document is None:
            Document.create(
                id=DOCUMENT_ID,
                kb_id=project.dataset_id,
                parser_id="naive",
                source_type="cloud-source",
                type="doc",
                suffix="doc",
                created_by=user.id,
                name="approved-word-e2e.doc",
                location=None,
            )
        elif document.kb_id != project.dataset_id or document.source_type != "cloud-source":
            fail("DOCMIND_E2E_DOCUMENT_CONFLICT")

        mapping = docmind_ingestion_service.register_source_document_mapping(
            user.id,
            project_id=project.id,
            source_id=source.id,
            document_id=DOCUMENT_ID,
            folder_id=folder.id,
            relative_path=relative_path,
        )

    print(
        json.dumps(
            {
                "ready": True,
                "tenant_id": user.id,
                "project_id": project.id,
                "dataset_id": project.dataset_id,
                "folder_id": folder.id,
                "source_id": source.id,
                "document_id": mapping.document_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - do not expose database or configuration details.
        fail("DOCMIND_E2E_SEED_FAILED")
