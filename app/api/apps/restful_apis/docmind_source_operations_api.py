import logging

from quart import request

from api.apps import login_required
from api.apps.services import docmind_source_operation_service as source_operations
from api.utils.api_utils import add_tenant_id_to_kwargs, get_error_data_result, get_result

logger = logging.getLogger(__name__)


async def _body(allowed: set[str], required: set[str]) -> dict:
    value = await request.get_json(silent=True)
    if not isinstance(value, dict) or set(value) - allowed or not required.issubset(value):
        raise source_operations.SourceOperationError("SOURCE_OPERATION_REQUEST_INVALID", 400)
    return value


def _error(error: source_operations.SourceOperationError):
    return get_error_data_result(message=error.code), error.status


def _submitted(result: tuple[dict[str, object], int]):
    payload, status = result
    return get_result(data=payload), status


@manager.route("/docmind/source-operations/capabilities", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def source_operation_capabilities(tenant_id: str):
    try:
        return get_result(data=source_operations.capabilities(tenant_id, request.args.get("source_id")))
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source operation capabilities failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500


@manager.route("/docmind/documents/<document_id>", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def get_source_document(tenant_id: str, document_id: str):
    try:
        return get_result(data=source_operations.get_document(tenant_id, document_id))
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source document lookup failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500


@manager.route("/docmind/folders/<folder_id>/children", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def list_source_folder_children(tenant_id: str, folder_id: str):
    try:
        return get_result(data=source_operations.list_folder_children(tenant_id, folder_id))
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source folder listing failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500


@manager.route("/docmind/documents", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def create_source_document(tenant_id: str):
    try:
        payload = await _body(
            {"source_id", "destination_folder_id", "name", "upload_token", "content_sha256"},
            {"source_id", "destination_folder_id", "name", "upload_token", "content_sha256"},
        )
        return _submitted(
            source_operations.submit_operation(
                tenant_id,
                "CREATE",
                payload,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        )
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source document creation failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500


@manager.route("/docmind/documents/<document_id>/content", methods=["PUT"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def update_source_document_content(tenant_id: str, document_id: str):
    try:
        payload = await _body(
            {"expected_source_version_id", "upload_token", "content_sha256"},
            {"expected_source_version_id", "upload_token", "content_sha256"},
        )
        payload["document_id"] = document_id
        return _submitted(
            source_operations.submit_operation(
                tenant_id,
                "UPDATE_CONTENT",
                payload,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        )
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source document content update failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500


@manager.route("/docmind/documents/<document_id>", methods=["PATCH"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def rename_source_document(tenant_id: str, document_id: str):
    try:
        payload = await _body(
            {"expected_source_version_id", "name"},
            {"expected_source_version_id", "name"},
        )
        payload["document_id"] = document_id
        return _submitted(
            source_operations.submit_operation(
                tenant_id,
                "RENAME",
                payload,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        )
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source document rename failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500


@manager.route("/docmind/documents/<document_id>", methods=["DELETE"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def delete_source_document(tenant_id: str, document_id: str):
    try:
        payload = await _body({"expected_source_version_id"}, {"expected_source_version_id"})
        payload["document_id"] = document_id
        return _submitted(
            source_operations.submit_operation(
                tenant_id,
                "DELETE",
                payload,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        )
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source document deletion failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500


@manager.route("/docmind/documents/<document_id>/copy", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def copy_source_document(tenant_id: str, document_id: str):
    try:
        payload = await _body(
            {"expected_source_version_id", "destination_source_id", "destination_folder_id", "name"},
            {"expected_source_version_id", "destination_folder_id"},
        )
        payload["document_id"] = document_id
        return _submitted(
            source_operations.submit_operation(
                tenant_id,
                "COPY",
                payload,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        )
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source document copy failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500


@manager.route("/docmind/documents/<document_id>/move", methods=["POST"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def move_source_document(tenant_id: str, document_id: str):
    try:
        payload = await _body(
            {"expected_source_version_id", "destination_source_id", "destination_folder_id", "name"},
            {"expected_source_version_id", "destination_folder_id"},
        )
        payload["document_id"] = document_id
        return _submitted(
            source_operations.submit_operation(
                tenant_id,
                "MOVE",
                payload,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        )
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source document move failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500


@manager.route("/docmind/operations/<operation_id>", methods=["GET"])  # noqa: F821
@login_required
@add_tenant_id_to_kwargs
async def get_source_operation(tenant_id: str, operation_id: str):
    try:
        return get_result(data=source_operations.get_operation(tenant_id, operation_id))
    except source_operations.SourceOperationError as error:
        return _error(error)
    except Exception:
        logger.exception("DocMind source operation lookup failed")
        return get_error_data_result(message="SOURCE_OPERATION_INTERNAL_ERROR"), 500
