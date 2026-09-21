import logging

from quart import request

from api.apps import login_required, login_user
from api.apps.services import (
    docmind_api_service,
    docmind_bootstrap_service,
    docmind_draft_service,
    docmind_generation_service,
    docmind_hierarchy_service,
    docmind_publish_service,
    docmind_registration_service,
    docmind_shared_workspace_service,
)
from api.db.services.document_service import DocmindProtectedEvidenceError
from api.utils.api_utils import add_tenant_id_to_kwargs, get_error_argument_result, get_error_data_result, get_result

logger = logging.getLogger(__name__)


def _public_search_result(result):
    return {key: value for key, value in result.items() if key != "scope_doc_ids"}


@manager.route("/docmind/shared-session", methods=["POST"])  # noqa: F821
async def shared_workspace_session():
    if not docmind_shared_workspace_service.enabled():
        return (
            get_error_data_result(message="DOCMIND_SHARED_WORKSPACE_DISABLED"),
            404,
        )
    try:
        user = docmind_shared_workspace_service.resolve_user()
        if not login_user(user):
            raise docmind_shared_workspace_service.DocmindSharedWorkspaceError("DOCMIND_SHARED_USER_INACTIVE")
        response = get_result(data={"mode": "shared", "ready": True})
        response.headers["Cache-Control"] = "no-store"
        return response
    except docmind_shared_workspace_service.DocmindSharedWorkspaceError as error:
        logger.error("DocMind shared workspace session failed code=%s", error.code)
        return get_error_data_result(message=error.code), 503
    except Exception:
        logger.exception("DocMind shared workspace session failed")
        return get_error_data_result(message="DOCMIND_SHARED_WORKSPACE_INTERNAL_ERROR"), 503


@manager.route("/docmind/bootstrap", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def bootstrap_workspace(tenant_id: str):
    try:
        return get_result(data=docmind_bootstrap_service.bootstrap_shared_workspace(tenant_id))
    except docmind_bootstrap_service.DocmindBootstrapError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind workspace bootstrap failed")
        return get_error_data_result(message="DOCMIND_BOOTSTRAP_INTERNAL_ERROR")


@manager.route("/docmind/folders", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def folders(tenant_id: str):
    try:
        data = docmind_api_service.list_folders(tenant_id)
        data["can_administer"] = docmind_registration_service.can_administer(tenant_id)
        data["workspace_mode"] = "shared" if docmind_shared_workspace_service.enabled() else "authenticated"
        return get_result(data=data)
    except docmind_api_service.DocmindCatalogNotInitializedError:
        workspace = docmind_bootstrap_service.workspace_status(tenant_id)
        return get_result(
            data={
                "dataset_id": str(workspace["dataset_id"]) if workspace else "",
                "catalog_source": "database",
                "catalog_version_id": "",
                "folders": [],
                "initialized": False,
                "can_administer": bool(workspace),
                "workspace_mode": (
                    "shared"
                    if docmind_shared_workspace_service.enabled()
                    else "authenticated"
                ),
            }
        )
    except Exception as error:
        logger.exception("DocMind folder list failed")
        return get_error_data_result(message=str(error))


@manager.route("/docmind/search", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def search(tenant_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    question = str((req or {}).get("question") or "").strip()
    if not question:
        return get_error_argument_result("question is required")
    try:
        search_args = {}
        if "folder_ids" in req:
            search_args["folder_ids"] = req["folder_ids"]
            if "catalog_version_id" in req:
                search_args["catalog_version_id"] = req["catalog_version_id"]
        elif "catalog_version_id" in req:
            return get_error_argument_result("catalog_version_id requires folder_ids")
        result = await docmind_api_service.search(tenant_id, question, **search_args)
        return get_result(data=_public_search_result(result))
    except docmind_api_service.DocmindCatalogNotInitializedError as error:
        return get_error_data_result(message=error.code)
    except ValueError as error:
        return get_error_argument_result(str(error))
    except Exception as error:
        logger.exception("DocMind search failed")
        return get_error_data_result(message=str(error))


@manager.route("/docmind/admin/registrations", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def create_registrations(tenant_id: str):
    form = await request.form
    files = await request.files
    folder_id = str(form.get("folder_id") or "").strip()
    file_objects = files.getlist("file") if "file" in files else []
    try:
        result = await docmind_registration_service.register_documents(
            tenant_id,
            folder_id,
            file_objects,
        )
        return get_result(data=result)
    except docmind_registration_service.DocmindRegistrationError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind registration failed")
        return get_error_data_result(message="DOCMIND_REGISTRATION_INTERNAL_ERROR")


@manager.route("/docmind/admin/registrations", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def registrations(tenant_id: str):
    states = request.args.getlist("state")
    try:
        return get_result(
            data=docmind_registration_service.list_registrations(
                tenant_id,
                states=states or None,
            )
        )
    except docmind_registration_service.DocmindRegistrationError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind registration list failed")
        return get_error_data_result(message="DOCMIND_REGISTRATION_INTERNAL_ERROR")


@manager.route("/docmind/admin/hierarchy", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def hierarchy(tenant_id: str):
    try:
        return get_result(data=docmind_hierarchy_service.list_hierarchy(tenant_id))
    except docmind_hierarchy_service.DocmindHierarchyError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind hierarchy read failed")
        return get_error_data_result(message="DOCMIND_HIERARCHY_INTERNAL_ERROR")


@manager.route("/docmind/admin/hierarchy/imports", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def hierarchy_imports(tenant_id: str):
    try:
        return get_result(data=docmind_hierarchy_service.list_import_jobs(tenant_id))
    except docmind_hierarchy_service.DocmindHierarchyError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind hierarchy import list failed")
        return get_error_data_result(message="DOCMIND_HIERARCHY_INTERNAL_ERROR")


@manager.route("/docmind/admin/hierarchy/imports", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def import_hierarchy(tenant_id: str):
    form = await request.form
    files = await request.files
    paths = form.getlist("path")
    file_objects = files.getlist("file") if "file" in files else []
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        return get_result(
            data=await docmind_hierarchy_service.import_local_folder(
                tenant_id,
                paths,
                file_objects,
                idempotency_key,
            )
        )
    except docmind_hierarchy_service.DocmindHierarchyError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind hierarchy import failed")
        return get_error_data_result(message="DOCMIND_HIERARCHY_INTERNAL_ERROR")


@manager.route("/docmind/admin/hierarchy/imports/<job_id>/retry", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def retry_hierarchy_import(tenant_id: str, job_id: str):
    form = await request.form
    files = await request.files
    paths = form.getlist("path")
    file_objects = files.getlist("file") if "file" in files else []
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        return get_result(
            data=await docmind_hierarchy_service.retry_import(
                tenant_id,
                job_id,
                paths,
                file_objects,
                idempotency_key,
            )
        )
    except docmind_hierarchy_service.DocmindHierarchyError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind hierarchy import retry failed")
        return get_error_data_result(message="DOCMIND_HIERARCHY_INTERNAL_ERROR")


@manager.route("/docmind/admin/hierarchy/folders", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def create_hierarchy_folder(tenant_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    try:
        return get_result(
            data=docmind_hierarchy_service.create_folder(
                tenant_id,
                str(req.get("parent_file_id") or "").strip(),
                str(req.get("name") or "").strip(),
            )
        )
    except docmind_hierarchy_service.DocmindHierarchyError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind hierarchy folder creation failed")
        return get_error_data_result(message="DOCMIND_HIERARCHY_INTERNAL_ERROR")


@manager.route("/docmind/admin/hierarchy/folders/<folder_id>", methods=["PATCH"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def update_hierarchy_folder(tenant_id: str, folder_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    try:
        return get_result(
            data=docmind_hierarchy_service.update_folder(
                tenant_id,
                folder_id,
                parent_file_id=(str(req["parent_file_id"]).strip() if "parent_file_id" in req else None),
                name=(str(req["name"]).strip() if "name" in req else None),
            )
        )
    except docmind_hierarchy_service.DocmindHierarchyError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind hierarchy folder update failed")
        return get_error_data_result(message="DOCMIND_HIERARCHY_INTERNAL_ERROR")


@manager.route("/docmind/admin/hierarchy/folders/<folder_id>", methods=["DELETE"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def delete_hierarchy_folder(tenant_id: str, folder_id: str):
    try:
        return get_result(data=docmind_hierarchy_service.delete_folder(tenant_id, folder_id))
    except docmind_hierarchy_service.DocmindHierarchyError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind hierarchy folder deletion failed")
        return get_error_data_result(message="DOCMIND_HIERARCHY_INTERNAL_ERROR")


@manager.route("/docmind/admin/hierarchy/capture", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def capture_hierarchy(tenant_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        return get_result(
            data=docmind_hierarchy_service.capture_hierarchy_draft(
                tenant_id,
                str(req.get("expected_active_version_id") or "").strip(),
                idempotency_key,
            )
        )
    except docmind_hierarchy_service.DocmindHierarchyError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind hierarchy capture failed")
        return get_error_data_result(message="DOCMIND_HIERARCHY_INTERNAL_ERROR")


@manager.route("/docmind/admin/registrations/<registration_id>/retry", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def retry_registration(tenant_id: str, registration_id: str):
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        return get_result(
            data=await docmind_registration_service.retry_registration(
                tenant_id,
                registration_id,
                idempotency_key,
            )
        )
    except docmind_registration_service.DocmindRegistrationError as error:
        return get_error_data_result(message=error.code)
    except DocmindProtectedEvidenceError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind registration retry failed")
        return get_error_data_result(message="DOCMIND_REGISTRATION_INTERNAL_ERROR")


@manager.route("/docmind/admin/catalog/drafts", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def catalog_drafts(tenant_id: str):
    try:
        return get_result(data=docmind_draft_service.list_drafts(tenant_id))
    except docmind_draft_service.DocmindDraftError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind draft list failed")
        return get_error_data_result(message="DOCMIND_DRAFT_INTERNAL_ERROR")


@manager.route("/docmind/admin/catalog/drafts", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def create_catalog_draft(tenant_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    expected_parent_version_id = str(req.get("expected_parent_version_id") or "").strip()
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        return get_result(
            data=docmind_draft_service.create_draft(
                tenant_id,
                expected_parent_version_id,
                idempotency_key,
            )
        )
    except docmind_draft_service.DocmindDraftError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind draft creation failed")
        return get_error_data_result(message="DOCMIND_DRAFT_INTERNAL_ERROR")


@manager.route("/docmind/admin/catalog/drafts/<draft_id>", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def catalog_draft(tenant_id: str, draft_id: str):
    try:
        return get_result(data=docmind_draft_service.get_draft(tenant_id, draft_id))
    except docmind_draft_service.DocmindDraftError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind draft read failed")
        return get_error_data_result(message="DOCMIND_DRAFT_INTERNAL_ERROR")


@manager.route("/docmind/admin/catalog/drafts/<draft_id>/changes", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def change_catalog_draft(tenant_id: str, draft_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        return get_result(
            data=docmind_draft_service.apply_change(
                tenant_id,
                draft_id,
                str(req.get("operation") or ""),
                document_id=str(req.get("document_id") or "").strip(),
                registration_id=(str(req.get("registration_id") or "").strip() or None),
                from_folder_id=(str(req.get("from_folder_id") or "").strip() or None),
                to_folder_id=(str(req.get("to_folder_id") or "").strip() or None),
                expected_parent_folder_id=(str(req.get("expected_parent_folder_id") or "").strip() or None),
                idempotency_key=idempotency_key,
            )
        )
    except docmind_draft_service.DocmindDraftError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind draft change failed")
        return get_error_data_result(message="DOCMIND_DRAFT_INTERNAL_ERROR")


@manager.route(  # noqa: F821
    "/docmind/admin/catalog/drafts/<draft_id>/generate",
    methods=["POST"],
)  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def generate_catalog_draft(tenant_id: str, draft_id: str):
    try:
        return get_result(
            data=docmind_generation_service.start_generation(
                tenant_id,
                draft_id,
            )
        )
    except (
        docmind_draft_service.DocmindDraftError,
        docmind_generation_service.DocmindGenerationError,
    ) as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind draft generation failed to start")
        return get_error_data_result(message="DOCMIND_GENERATION_INTERNAL_ERROR")


@manager.route(  # noqa: F821
    "/docmind/admin/catalog/drafts/<source_ready_id>/manual-card-revisions",
    methods=["POST"],
)  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def create_manual_card_revision(tenant_id: str, source_ready_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        return get_result(
            data=docmind_draft_service.create_manual_card_revision(
                tenant_id,
                source_ready_id,
                expected_active_version_id=str(req.get("expected_active_version_id") or "").strip(),
                expected_source_snapshot_hash=str(req.get("expected_source_snapshot_hash") or "").strip(),
                cards=req.get("cards"),
                idempotency_key=idempotency_key,
            )
        )
    except docmind_draft_service.DocmindDraftError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind manual card revision failed")
        return get_error_data_result(message="DOCMIND_MANUAL_CARD_INTERNAL_ERROR")


@manager.route("/docmind/admin/catalog/versions", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def catalog_versions(tenant_id: str):
    try:
        return get_result(data=docmind_publish_service.list_versions(tenant_id))
    except docmind_publish_service.DocmindPublishError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind version list failed")
        return get_error_data_result(message="DOCMIND_PUBLISH_INTERNAL_ERROR")


@manager.route(  # noqa: F821
    "/docmind/admin/catalog/versions/<version_id>",
    methods=["DELETE"],
)  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def delete_catalog_version(tenant_id: str, version_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    try:
        return get_result(
            data=docmind_publish_service.queue_failed_version_deletion(
                tenant_id,
                version_id,
                expected_active_version_id=str(req.get("expected_active_version_id") or "").strip(),
                expected_version_label=str(req.get("expected_version_label") or "").strip(),
            )
        )
    except docmind_publish_service.DocmindPublishError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind version deletion failed")
        return get_error_data_result(message="DOCMIND_VERSION_DELETE_INTERNAL_ERROR")


@manager.route(  # noqa: F821
    "/docmind/admin/catalog/drafts/<version_id>/publish",
    methods=["POST"],
)  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def publish_catalog_version(tenant_id: str, version_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    expected_active_version_id = str(req.get("expected_active_version_id") or "").strip()
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        return get_result(
            data=docmind_publish_service.publish_version(
                tenant_id,
                version_id,
                expected_active_version_id,
                idempotency_key,
            )
        )
    except docmind_publish_service.DocmindPublishError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind version publish failed")
        return get_error_data_result(message="DOCMIND_PUBLISH_INTERNAL_ERROR")


@manager.route(  # noqa: F821
    "/docmind/admin/catalog/<version_id>/rollback",
    methods=["POST"],
)  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def rollback_catalog_version(tenant_id: str, version_id: str):
    req = await request.get_json(silent=True)
    if not isinstance(req, dict):
        return get_error_argument_result("JSON body must be an object")
    expected_active_version_id = str(req.get("expected_active_version_id") or "").strip()
    idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
    try:
        return get_result(
            data=docmind_publish_service.rollback_version(
                tenant_id,
                version_id,
                expected_active_version_id,
                idempotency_key,
            )
        )
    except docmind_publish_service.DocmindPublishError as error:
        return get_error_data_result(message=error.code)
    except Exception:
        logger.exception("DocMind version rollback failed")
        return get_error_data_result(message="DOCMIND_PUBLISH_INTERNAL_ERROR")
