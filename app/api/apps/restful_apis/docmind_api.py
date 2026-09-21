import asyncio
import logging
import os

from quart import Response, request

from api.apps import login_required, login_user
from api.apps.services import (
    docmind_api_service,
    docmind_bootstrap_service,
    docmind_hierarchy_service,
    docmind_ingestion_service,
    docmind_registration_service,
    docmind_shared_workspace_service,
    docmind_worker_auth,
)
from api.db.services.document_service import DocmindProtectedEvidenceError
from api.utils.api_utils import add_tenant_id_to_kwargs, get_error_argument_result, get_error_data_result, get_result

logger = logging.getLogger(__name__)


def _worker_path_and_query() -> str:
    query = request.query_string.decode("ascii")
    return request.path + (f"?{query}" if query else "")


async def _authenticate_worker(body: bytes) -> str:
    key_id = str(request.headers.get("X-DocMind-Key-Id") or "")
    docmind_worker_auth.verify(
        method=request.method,
        path_and_query=_worker_path_and_query(),
        body=body,
        key_id=key_id,
        timestamp=str(request.headers.get("X-DocMind-Timestamp") or ""),
        nonce=str(request.headers.get("X-DocMind-Nonce") or ""),
        content_sha256=str(request.headers.get("X-DocMind-Content-SHA256") or "").lower(),
        signature=str(request.headers.get("X-DocMind-Signature") or "").lower(),
    )
    return key_id


def _signed_worker_response(payload: dict, key_id: str, status: int = 200) -> Response:
    body, headers = docmind_worker_auth.signed_json_body(
        payload,
        method=request.method,
        path_and_query=_worker_path_and_query(),
        key_id=key_id,
    )
    return Response(body, status=status, content_type="application/json", headers=headers)


def _public_search_result(result):
    return {key: value for key, value in result.items() if key != "scope_doc_ids"}


def _parse_search_scope(value):
    if not isinstance(value, dict):
        raise ValueError("scope must be an object")

    mode = value.get("mode")
    allowed_keys = {
        "all": {"mode"},
        "folders": {"mode", "folder_ids"},
        "documents": {"mode", "document_ids"},
    }
    if mode not in allowed_keys:
        raise ValueError("scope.mode must be one of: all, folders, documents")
    unexpected = set(value) - allowed_keys[mode]
    if unexpected:
        raise ValueError(f"scope contains fields invalid for {mode} mode")

    if mode == "all":
        return {"mode": "all"}

    ids_key = "folder_ids" if mode == "folders" else "document_ids"
    identifiers = value.get(ids_key)
    if not isinstance(identifiers, list) or not identifiers:
        raise ValueError(f"scope.{ids_key} must be a non-empty array")
    if any(not isinstance(identifier, str) or not identifier.strip() for identifier in identifiers):
        raise ValueError(f"scope.{ids_key} must contain non-empty strings")
    return {"mode": mode, ids_key: [identifier.strip() for identifier in identifiers]}


@manager.route("/cloud-sync/host-worker/observations", methods=["POST"])  # noqa: F821
async def observe_cloud_sync_source_version():
    body = await request.get_data()
    key_id = ""
    try:
        key_id = await _authenticate_worker(body)
        req = await request.get_json(silent=True)
        expected = {
            "source_id",
            "document_id",
            "relative_path",
            "ciphertext_sha256",
            "size",
            "mtime_ns",
        }
        if not isinstance(req, dict) or set(req) != expected:
            raise docmind_ingestion_service.DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID")
        if not isinstance(req["size"], int) or not isinstance(req["mtime_ns"], int):
            raise docmind_ingestion_service.DocmindIngestionError("DOCMIND_INGESTION_OBSERVATION_INVALID")
        result = docmind_ingestion_service.observe_source_version_from_worker(
            source_id=req["source_id"],
            document_id=req["document_id"],
            relative_path=req["relative_path"],
            ciphertext_sha256=req["ciphertext_sha256"],
            ciphertext_size=req["size"],
            source_mtime_ns=req["mtime_ns"],
        )
        return _signed_worker_response(result, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_ingestion_service.DocmindIngestionError as error:
        return _signed_worker_response({"error": error.code}, key_id, 409)
    except Exception:
        logger.exception("DocMind host worker observation failed")
        if key_id:
            return _signed_worker_response({"error": "DOCMIND_INGESTION_INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")


@manager.route("/cloud-sync/host-worker/claim", methods=["POST"])  # noqa: F821
async def claim_cloud_sync_job():
    body = await request.get_data()
    key_id = ""
    try:
        key_id = await _authenticate_worker(body)
        req = await request.get_json(silent=True)
        if (
            not isinstance(req, dict)
            or set(req) - {"worker_id", "protocol_version", "lease_seconds"}
            or req.get("protocol_version") != 1
        ):
            raise docmind_ingestion_service.DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID")
        worker_id = str(req.get("worker_id") or "")
        lease_seconds = req.get("lease_seconds", 300)
        if not isinstance(lease_seconds, int):
            raise docmind_ingestion_service.DocmindIngestionError("DOCMIND_INGESTION_LEASE_INVALID")
        docmind_ingestion_service.maintain_parser_workspaces()
        job = docmind_ingestion_service.claim_next(worker_id, lease_seconds=lease_seconds)
        return _signed_worker_response({"job": job.to_dict() if job else None}, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_ingestion_service.DocmindIngestionError as error:
        return _signed_worker_response({"error": error.code}, key_id, 409)
    except Exception:
        logger.exception("DocMind host worker claim failed")
        if key_id:
            return _signed_worker_response({"error": "DOCMIND_INGESTION_INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")


@manager.route("/cloud-sync/host-worker/jobs/<job_id>/artifact", methods=["PUT"])  # noqa: F821
async def upload_cloud_sync_artifact(job_id: str):
    key_id = ""
    try:
        maximum = int(os.getenv("DOCMIND_HOST_WORKER_MAX_ARTIFACT_BYTES", str(256 * 1024 * 1024)))
        if request.content_length is None:
            return Response(b'{"error":"CONTENT_LENGTH_REQUIRED"}', status=411, content_type="application/json")
        if request.content_length < 0 or request.content_length > maximum:
            return Response(b'{"error":"ARTIFACT_TOO_LARGE"}', status=413, content_type="application/json")
        # Content-Length is checked before buffering. The explicit cap bounds
        # memory until the parser runtime adopts a streaming adapter contract.
        body = await request.get_data()
        key_id = await _authenticate_worker(body)
        if len(body) > maximum:
            return _signed_worker_response({"error": "ARTIFACT_TOO_LARGE"}, key_id, 413)
        worker_id = str(request.headers.get("X-DocMind-Worker-Id") or "")
        version_id = str(request.headers.get("X-DocMind-Version-Id") or "")
        plaintext_sha256 = str(request.headers.get("X-DocMind-Plaintext-SHA256") or "").lower()
        try:
            fencing_token = int(request.headers.get("X-DocMind-Fencing-Token") or "")
            plaintext_size = int(request.headers.get("X-DocMind-Plaintext-Size") or "")
        except ValueError as error:
            raise docmind_ingestion_service.DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID") from error
        adapter, runner, activator = docmind_ingestion_service.ingestion_runtime()
        result = await asyncio.to_thread(
            docmind_ingestion_service.process_decrypted_artifact,
            job_id,
            worker_id=worker_id,
            version_id=version_id,
            fencing_token=fencing_token,
            plaintext=body,
            plaintext_sha256=plaintext_sha256,
            plaintext_size=plaintext_size,
            adapter=adapter,
            runner=runner,
            activator=activator,
        )
        return _signed_worker_response(result, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_ingestion_service.DocmindIngestionError as error:
        return _signed_worker_response({"error": error.code}, key_id, 409)
    except Exception:
        logger.exception("DocMind host worker artifact upload failed")
        if key_id:
            return _signed_worker_response({"error": "DOCMIND_INGESTION_INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")


@manager.route("/cloud-sync/host-worker/jobs/<job_id>/status", methods=["POST"])  # noqa: F821
async def update_cloud_sync_job_status(job_id: str):
    body = await request.get_data()
    key_id = ""
    try:
        key_id = await _authenticate_worker(body)
        req = await request.get_json(silent=True)
        allowed = {"worker_id", "version_id", "fencing_token", "status", "error_code"}
        if not isinstance(req, dict) or set(req) - allowed:
            raise docmind_ingestion_service.DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID")
        if not isinstance(req.get("fencing_token"), int):
            raise docmind_ingestion_service.DocmindIngestionError("DOCMIND_INGESTION_REQUEST_INVALID")
        result = docmind_ingestion_service.record_worker_status(
            job_id,
            worker_id=str(req.get("worker_id") or ""),
            version_id=str(req.get("version_id") or ""),
            fencing_token=req["fencing_token"],
            status=str(req.get("status") or ""),
            error_code=(str(req["error_code"]) if req.get("error_code") else None),
        )
        return _signed_worker_response(result, key_id)
    except docmind_worker_auth.WorkerAuthenticationError:
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")
    except docmind_ingestion_service.DocmindIngestionError as error:
        return _signed_worker_response({"error": error.code}, key_id, 409)
    except Exception:
        logger.exception("DocMind host worker status update failed")
        if key_id:
            return _signed_worker_response({"error": "DOCMIND_INGESTION_INTERNAL_ERROR"}, key_id, 500)
        return Response(b'{"error":"WORKER_AUTH_FAILED"}', status=401, content_type="application/json")


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
    unexpected = set(req) - {"question", "project_id", "scope"}
    if unexpected:
        return get_error_argument_result("request contains unsupported fields")
    question_value = req.get("question")
    if not isinstance(question_value, str) or not question_value.strip():
        return get_error_argument_result("question is required")
    question = question_value.strip()
    try:
        project_id = None
        if "project_id" in req:
            if not isinstance(req["project_id"], str) or not req["project_id"].strip():
                raise ValueError("project_id must be a non-empty string")
            project_id = req["project_id"].strip()
        scope = _parse_search_scope(req.get("scope"))
        result = await docmind_api_service.search(
            tenant_id,
            question,
            project_id=project_id,
            scope=scope,
        )
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
