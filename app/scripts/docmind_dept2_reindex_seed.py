"""Create only the logical DEPT2 source needed by the real-input reindex.

No physical path, document mapping, plaintext, or secret is stored here.
The Windows watcher discovers documents after this source has been provisioned.
"""

from __future__ import annotations

import json
import os

from api.apps.services import docmind_bootstrap_service
from api.apps.services.docmind_shared_workspace_service import resolve_user
from api.db.db_models import DB, DocmindFolder, DocmindProject, DocmindSource, Knowledgebase
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService

SOURCE_ID = "dept-2-e2e"
FOLDER_ID = "17e6a865915e2bf7e752b97eb15fd6d0"
EMBEDDING_MODEL = "BAAI/bge-m3@Builtin"


def main() -> None:
    if os.environ.get("DOCMIND_DEV_APPROVED_SOURCE_ID") != SOURCE_ID:
        raise RuntimeError("DEPT2_REINDEX_SOURCE_NOT_APPROVED")

    with DB.connection_context():
        user = resolve_user()
        workspace = docmind_bootstrap_service.bootstrap_shared_workspace(user.id)
        project = DocmindProject.get_by_id(str(workspace["project_id"]))
        dataset = Knowledgebase.get_by_id(project.dataset_id)
        tenant_ok, tenant = TenantService.get_by_id(user.id)
        if not tenant_ok or tenant is None:
            raise RuntimeError("DEPT2_REINDEX_TENANT_MISSING")
        tenant_embedding = str(tenant.embd_id or "").strip() or EMBEDDING_MODEL
        if not str(tenant.embd_id or "").strip():
            TenantService.update_by_id(user.id, {"embd_id": tenant_embedding, "tenant_embd_id": None})
        if not str(dataset.embd_id or "").strip():
            KnowledgebaseService.update_by_id(
                dataset.id, {"embd_id": tenant_embedding, "tenant_embd_id": None}
            )

        folder = DocmindFolder.get_or_none(DocmindFolder.id == FOLDER_ID)
        if folder is None:
            folder = DocmindFolder.create(
                id=FOLDER_ID,
                project_id=project.id,
                slug="dept2-reindex",
                display_name="DEPT2",
                ordinal=0,
            )
        elif folder.project_id != project.id:
            raise RuntimeError("DEPT2_REINDEX_FOLDER_CONFLICT")

        source = DocmindSource.get_or_none(DocmindSource.id == SOURCE_ID)
        if source is None:
            source = DocmindSource.create(
                id=SOURCE_ID,
                project_id=project.id,
                display_name="T: (DEPT2)",
                default_folder_id=folder.id,
            )
        elif source.project_id != project.id or source.default_folder_id != folder.id:
            raise RuntimeError("DEPT2_REINDEX_SOURCE_CONFLICT")

    print(json.dumps({"ready": True, "source_id": SOURCE_ID, "folder_id": FOLDER_ID}))


if __name__ == "__main__":
    main()
