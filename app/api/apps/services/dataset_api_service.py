#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import logging
import math
import re

from api.db.db_models import Document, File
from api.db.joint_services.tenant_model_service import (
    get_composite_model_name_by_ids,
    resolve_model_config,
    resolve_model_id,
)
from api.db.services.document_service import DocumentService, DocmindProtectedEvidenceError
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService, validate_dataset_embedding_models
from api.db.services.tenant_model_service import TenantModelService
from api.db.services.user_service import TenantService, UserService, UserTenantService
from api.utils.api_utils import deep_merge, get_parser_config, remap_dictionary_keys, verify_embedding_availability
from common import settings
from common.constants import PAGERANK_FLD, FileSource, LLMType, StatusEnum
from common.misc_utils import thread_pool_exec_long_time

async def create_dataset(tenant_id: str, req: dict):
    """
    Create a new dataset.

    :param tenant_id: tenant ID
    :param req: dataset creation request
    :return: (success, result) or (success, error_message)
    """
    if "connectors" in req or "connectors" in (req.get("ext") or {}):
        return False, "External connectors are not supported"

    # Extract ext field for additional parameters
    ext_fields = req.pop("ext", {})

    # Map auto_metadata_config (if provided) into parser_config structure
    auto_meta = req.pop("auto_metadata_config", {})
    if auto_meta:
        parser_cfg = req.get("parser_config") or {}
        fields = []
        for f in auto_meta.get("fields", []):
            fields.append(
                {
                    "name": f.get("name", ""),
                    "type": f.get("type", ""),
                    "description": f.get("description"),
                    "examples": f.get("examples"),
                    "restrict_values": f.get("restrict_values", False),
                }
            )
        parser_cfg["metadata"] = fields
        parser_cfg["enable_metadata"] = auto_meta.get("enabled", True)
        req["parser_config"] = parser_cfg
    req.update(ext_fields)

    e, create_dict = KnowledgebaseService.create_with_name(name=req.pop("name", None), tenant_id=tenant_id, parser_id=req.pop("parser_id", None), **req)

    if not e:
        return False, create_dict

    # Insert embedding model(embd id)
    ok, t = TenantService.get_by_id(tenant_id)
    if not ok:
        return False, "Tenant not found"
    if not create_dict.get("embd_id"):
        create_dict["embd_id"] = t.embd_id
    else:
        ok, err = verify_embedding_availability(create_dict["embd_id"], tenant_id)
        if not ok:
            return False, err

    if not KnowledgebaseService.save(**create_dict):
        return False, "Failed to save dataset"
    ok, k = KnowledgebaseService.get_by_id(create_dict["id"])
    if not ok:
        return False, "Dataset created failed"
    response_data = remap_dictionary_keys(k.to_dict())
    return True, response_data


def _delete_datasets_sync(tenant_id: str, ids: list = None, delete_all: bool = False):
    """
    Delete datasets.

    :param tenant_id: tenant ID
    :param ids: list of dataset IDs
    :param delete_all: whether to delete all datasets of the tenant (if ids is not provided)
    :return: (success, result) or (success, error_message)
    """
    kb_id_instance_pairs = []
    if not ids:
        if not delete_all:
            return True, {"success_count": 0}
        else:
            ids = [kb.id for kb in KnowledgebaseService.query(tenant_id=tenant_id)]

    error_kb_ids = []
    for kb_id in ids:
        kb = KnowledgebaseService.get_or_none(id=kb_id, tenant_id=tenant_id)
        if kb is None:
            error_kb_ids.append(kb_id)
            continue
        kb_id_instance_pairs.append((kb_id, kb))
    if len(error_kb_ids) > 0:
        return False, f"""User '{tenant_id}' lacks permission for datasets: '{", ".join(error_kb_ids)}'"""

    all_document_ids = [
        document.id
        for kb_id, _kb in kb_id_instance_pairs
        for document in DocumentService.query(kb_id=kb_id)
    ]
    try:
        DocumentService.assert_documents_docmind_evidence_mutable(
            all_document_ids,
            "DELETE_DATASET_BATCH",
            actor_id=tenant_id,
        )
    except DocmindProtectedEvidenceError as error:
        return False, error.code

    errors = []
    success_count = 0
    for kb_id, kb in kb_id_instance_pairs:
        for doc in DocumentService.query(kb_id=kb_id):
            if not DocumentService.remove_document(doc, tenant_id):
                errors.append(f"Remove document '{doc.id}' error for dataset '{kb_id}'")
                continue
            f2d = File2DocumentService.get_by_document_id(doc.id)
            if f2d:
                FileService.filter_delete(
                    [
                        File.source_type == FileSource.KNOWLEDGEBASE,
                        File.id == f2d[0].file_id,
                    ]
                )
            else:
                # Normal uploads create a File2Document row via FileService.add_file_from_kb.
                # A missing row usually means stale/partial data (e.g. link removed earlier,
                # failed post-insert file linkage, or legacy rows). Deletion still proceeds.
                logging.warning(
                    "delete_datasets: document %s in dataset %s has no File2Document row; skipping linked file delete",
                    doc.id,
                    kb_id,
                )
            File2DocumentService.delete_by_document_id(doc.id)
        FileService.filter_delete([File.source_type == FileSource.KNOWLEDGEBASE, File.type == "folder", File.name == kb.name])

        # Drop index for this dataset
        try:
            from rag.nlp import search

            idxnm = search.index_name(kb.tenant_id)
            settings.docStoreConn.delete_idx(idxnm, kb_id)
        except Exception as e:
            errors.append(f"Failed to drop index for dataset {kb_id}: {e}")

        if not KnowledgebaseService.delete_by_id(kb_id):
            errors.append(f"Delete dataset error for {kb_id}")
            continue

        # Sweep any document rows the per-document loop could not remove.
        stranded = DocumentService.filter_delete([Document.kb_id == kb_id])
        if stranded:
            logging.warning("delete_datasets: removed %s stranded document rows for dataset %s", stranded, kb_id)

        success_count += 1

    if not errors:
        return True, {"success_count": success_count}

    error_message = f"Successfully deleted {success_count} datasets, {len(errors)} failed. Details: {'; '.join(errors)[:128]}..."
    if success_count == 0:
        return False, error_message

    return True, {"success_count": success_count, "errors": errors[:5]}


async def delete_datasets(tenant_id: str, ids: list = None, delete_all: bool = False):
    return await thread_pool_exec_long_time(_delete_datasets_sync, tenant_id, ids, delete_all)


def get_dataset(dataset_id: str, tenant_id: str):
    """
    Get a single dataset.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :return: (success, result) or (success, error_message)
    """
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'

    if not KnowledgebaseService.accessible(dataset_id, tenant_id):
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    ok, kb = KnowledgebaseService.get_by_id(dataset_id)
    if not ok:
        return False, "Invalid Dataset ID"

    response_data = remap_dictionary_keys(kb.to_dict())
    response_data["size"] = DocumentService.get_total_size_by_kb_id(dataset_id)
    return True, response_data


def get_ingestion_summary(dataset_id: str, tenant_id: str):
    """
    Get ingestion summary for a dataset.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :return: (success, result) or (success, error_message)
    """
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'

    if not KnowledgebaseService.accessible(dataset_id, tenant_id):
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    ok, kb = KnowledgebaseService.get_by_id(dataset_id)
    if not ok:
        return False, "Invalid Dataset ID"

    status = DocumentService.get_parsing_status_by_kb_ids([dataset_id]).get(dataset_id, {})
    return True, {
        "doc_num": kb.doc_num,
        "chunk_num": kb.chunk_num,
        "token_num": kb.token_num,
        "status": status,
    }


async def update_dataset(tenant_id: str, dataset_id: str, req: dict):
    """
    Update a dataset.

    :param tenant_id: tenant ID
    :param dataset_id: dataset ID
    :param req: dataset update request
    :return: (success, result) or (success, error_message)
    """
    if not req:
        return False, "no properties were modified"

    if "connectors" in req or "connectors" in (req.get("ext") or {}):
        return False, "External connectors are not supported"

    kbs = KnowledgebaseService.get_kb_by_id(dataset_id, tenant_id)
    if not kbs:
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    kb = KnowledgebaseService.get_or_none(id=dataset_id)
    if kb is None:
        return False, "Invalid Dataset ID"

    # Extract ext field for additional parameters
    ext_fields = req.pop("ext", {})

    # Map auto_metadata_config into parser_config if present
    auto_meta = req.pop("auto_metadata_config", {})
    if auto_meta:
        parser_cfg = req.get("parser_config") or {}
        fields = []
        for f in auto_meta.get("fields", []):
            fields.append(
                {
                    "name": f.get("name", ""),
                    "type": f.get("type", ""),
                    "description": f.get("description"),
                    "examples": f.get("examples"),
                    "restrict_values": f.get("restrict_values", False),
                }
            )
        parser_cfg["metadata"] = fields
        parser_cfg["enable_metadata"] = auto_meta.get("enabled", True)
        req["parser_config"] = parser_cfg

    # Merge ext fields with req
    req.update(ext_fields)

    if req.get("parser_config"):
        # Flatten parent_child config into children_delimiter for the execution layer
        pc = req["parser_config"].get("parent_child", {})
        if pc.get("use_parent_child"):
            req["parser_config"]["children_delimiter"] = pc.get("children_delimiter", "\n")
            req["parser_config"]["enable_children"] = pc.get("use_parent_child", True)
        else:
            req["parser_config"]["children_delimiter"] = ""
            req["parser_config"]["enable_children"] = False
            req["parser_config"]["parent_child"] = {}

        parser_config = req["parser_config"]
        req_ext_fields = parser_config.pop("ext", {})
        parser_config.update(req_ext_fields)
        req["parser_config"] = deep_merge(kb.parser_config, parser_config)

    if (chunk_method := req.get("parser_id")) and chunk_method != kb.parser_id:
        if not req.get("parser_config"):
            req["parser_config"] = get_parser_config(chunk_method, None)
    elif "parser_config" in req and not req["parser_config"]:
        del req["parser_config"]


    if "name" in req and req["name"].lower() != kb.name.lower():
        exists = KnowledgebaseService.get_or_none(name=req["name"], tenant_id=kb.tenant_id, status=StatusEnum.VALID.value)
        if exists:
            return False, f"Dataset name '{req['name']}' already exists"

    if "embd_id" in req:
        if not req["embd_id"]:
            req["embd_id"] = kb.embd_id
        ok, err = verify_embedding_availability(req["embd_id"], tenant_id)
        if not ok:
            return False, err
        ok, _ = TenantModelService.get_by_id(req["embd_id"])
        if ok:
            req["tenant_embd_id"] = req["embd_id"]
        else:
            req["tenant_embd_id"] = resolve_model_id(tenant_id, LLMType.EMBEDDING, req["embd_id"])

    if "pagerank" in req and req["pagerank"] != kb.pagerank:

        if req["pagerank"] > 0:
            from rag.nlp import search

            settings.docStoreConn.update({"kb_id": kb.id}, {PAGERANK_FLD: req["pagerank"]}, search.index_name(kb.tenant_id), kb.id)
        else:
            # Elasticsearch requires PAGERANK_FLD be non-zero!
            from rag.nlp import search

            settings.docStoreConn.update({"exists": PAGERANK_FLD}, {"remove": PAGERANK_FLD}, search.index_name(kb.tenant_id), kb.id)
    req.pop("parse_type", None)

    if not KnowledgebaseService.update_by_id(kb.id, req):
        return False, "Update dataset error.(Database error)"

    ok, k = KnowledgebaseService.get_by_id(kb.id)
    if not ok:
        return False, "Dataset updated failed"

    response_data = remap_dictionary_keys(k.to_dict())
    return True, response_data


def list_datasets(tenant_id: str, args: dict):
    """
    List datasets.

    :param tenant_id: tenant ID
    :param args: query arguments
    :return: (success, result) or (success, error_message)
    """
    kb_id = args.get("id")
    kb_ids = args.get("ids")
    name = args.get("name")
    page = int(args.get("page", 1))
    page_size = int(args.get("page_size", 30))
    ext_fields = args.get("ext", {})
    parser_id = ext_fields.get("parser_id")
    keywords = ext_fields.get("keywords", "")
    orderby = args.get("orderby", "create_time")
    desc_arg = args.get("desc", "true")
    if isinstance(desc_arg, str):
        desc = desc_arg.lower() != "false"
    elif isinstance(desc_arg, bool):
        desc = desc_arg
    else:
        # unknown type, default to True
        desc = True

    if kb_id and kb_ids:
        return False, f"Should not provide both 'id':{kb_id} and 'ids'{kb_ids}"
    if kb_id:
        kbs = KnowledgebaseService.get_kb_by_id(kb_id, tenant_id)
        if not kbs:
            return False, f"User '{tenant_id}' lacks permission for dataset '{kb_id}'"

    if name:
        kbs = KnowledgebaseService.get_kb_by_name(name, tenant_id)
        if not kbs:
            return False, f"User '{tenant_id}' lacks permission for dataset '{name}'"
    owner_ids = [owner_id.strip() for owner_id in ext_fields.get("owner_ids", []) if isinstance(owner_id, str) and owner_id.strip()]
    if owner_ids:
        tenants = TenantService.get_joined_tenants_by_user_id(tenant_id)
        allowed_tenant_ids = {m["tenant_id"] for m in tenants}
        allowed_tenant_ids.add(tenant_id)
        tenant_ids = [owner_id for owner_id in owner_ids if owner_id in allowed_tenant_ids]
        query_user_id = tenant_id if tenant_id in tenant_ids else ""
    else:
        tenants = TenantService.get_joined_tenants_by_user_id(tenant_id)
        tenant_ids = [m["tenant_id"] for m in tenants]
        query_user_id = tenant_id
    if kb_ids:
        accessible_ids = KnowledgebaseService.get_accessible_ids([m["tenant_id"] for m in tenants], tenant_id, kb_ids)
        denied_ids = [kb_id for kb_id in kb_ids if kb_id not in accessible_ids]
        if denied_ids:
            logging.warning("User '%s' lacks permission for datasets: '%s'", tenant_id, ", ".join(denied_ids))
        kb_ids = [kb_id for kb_id in kb_ids if kb_id in accessible_ids]
        if not kb_ids:
            return True, {"data": [], "total": 0}
    kbs, total = KnowledgebaseService.get_list(tenant_ids, query_user_id, page, page_size, orderby, desc, kb_id, name, keywords, parser_id, kb_ids)
    users = UserService.get_by_ids([m["tenant_id"] for m in kbs])
    user_map = {m.id: m.to_dict() for m in users}

    ips_arg = args.get("include_parsing_status", False)
    if isinstance(ips_arg, str):
        include_parsing_status = ips_arg.lower() not in ("false", "0", "")
    elif isinstance(ips_arg, bool):
        include_parsing_status = ips_arg
    else:
        include_parsing_status = bool(ips_arg)

    status_by_kb = {}
    if include_parsing_status and kbs:
        status_by_kb = DocumentService.get_parsing_status_by_kb_ids([kb["id"] for kb in kbs])

    response_data_list = []
    for kb in kbs:
        user_dict = user_map.get(kb["tenant_id"], {})
        kb.update({"nickname": user_dict.get("nickname", ""), "tenant_avatar": user_dict.get("avatar", "")})
        if status_by_kb:
            kb["parsing_status"] = status_by_kb.get(kb["id"], {})
        response_data_list.append(remap_dictionary_keys(kb))

    embed_model_names = get_composite_model_name_by_ids([m["embedding_model"] for m in response_data_list])
    for response_data in response_data_list:
        response_data["embedding_model_name"] = embed_model_names.get(response_data["embedding_model"], "")
    return True, {"data": response_data_list, "total": total}


def list_dataset_filters(tenant_id: str):
    tenants = TenantService.get_joined_tenants_by_user_id(tenant_id)
    tenant_ids = [m["tenant_id"] for m in tenants]
    owners = KnowledgebaseService.get_owner_filter(tenant_ids, tenant_id)
    return True, {"filter": {"owner": owners}, "total": sum(owner["count"] for owner in owners)}


def list_tags(dataset_id: str, tenant_id: str):
    """
    List tags for a dataset.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :return: (success, result) or (success, error_message)
    """
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'

    if not KnowledgebaseService.accessible(dataset_id, tenant_id):
        return False, "no authorization"

    tenants = UserTenantService.get_tenants_by_user_id(tenant_id)
    tags = []
    for tenant in tenants:
        tags += settings.retriever.all_tags(tenant["tenant_id"], [dataset_id])
    return True, tags


def aggregate_tags(dataset_ids: list[str], tenant_id: str):
    """
    Aggregate tags across multiple datasets.

    :param dataset_ids: list of dataset IDs
    :param tenant_id: tenant ID
    :return: (success, result) or (success, error_message)
    """
    if not dataset_ids:
        return False, 'Lack of "dataset_ids"'

    for dataset_id in dataset_ids:
        if not KnowledgebaseService.accessible(dataset_id, tenant_id):
            return False, f"No authorization for dataset '{dataset_id}'"

    dataset_ids_by_tenant = {}
    for dataset_id in dataset_ids:
        ok, kb = KnowledgebaseService.get_by_id(dataset_id)
        if not ok:
            return False, f"Invalid Dataset ID '{dataset_id}'"
        dataset_ids_by_tenant.setdefault(kb.tenant_id, []).append(dataset_id)

    merged = {}
    for kb_tenant_id, kb_ids in dataset_ids_by_tenant.items():
        for tag, count in settings.retriever.all_tags(kb_tenant_id, kb_ids):
            merged[tag] = merged.get(tag, 0) + count

    return True, [{"value": tag, "count": count} for tag, count in merged.items()]


def get_flattened_metadata(dataset_ids: list[str], tenant_id: str):
    """
    Get flattened metadata for datasets.

    :param dataset_ids: list of dataset IDs
    :param tenant_id: tenant ID
    :return: (success, result) or (success, error_message)
    """
    if not dataset_ids:
        return False, 'Lack of "dataset_ids"'

    for dataset_id in dataset_ids:
        if not KnowledgebaseService.accessible(dataset_id, tenant_id):
            return False, f"No authorization for dataset '{dataset_id}'"

    from api.db.services.doc_metadata_service import DocMetadataService

    return True, DocMetadataService.get_flatted_meta_by_kbs(dataset_ids)


def get_auto_metadata(dataset_id: str, tenant_id: str):
    """
    Get auto-metadata configuration for a dataset.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :return: (success, result) or (success, error_message)
    """
    kb = KnowledgebaseService.get_or_none(id=dataset_id, tenant_id=tenant_id)
    if kb is None:
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"
    parser_cfg = kb.parser_config or {}
    return True, {"metadata": parser_cfg.get("metadata") or [], "built_in_metadata": parser_cfg.get("built_in_metadata") or []}


async def update_auto_metadata(dataset_id: str, tenant_id: str, cfg: dict):
    """
    Update auto-metadata configuration for a dataset.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :param cfg: auto-metadata configuration
    :return: (success, result) or (success, error_message)
    """
    kb = KnowledgebaseService.get_or_none(id=dataset_id, tenant_id=tenant_id)
    if kb is None:
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    parser_cfg = kb.parser_config or {}
    parser_cfg["metadata"] = cfg.get("metadata")
    parser_cfg["built_in_metadata"] = cfg.get("built_in_metadata")

    if not KnowledgebaseService.update_by_id(kb.id, {"parser_config": parser_cfg}):
        return False, "Update auto-metadata error.(Database error)"

    return True, cfg


def delete_tags(dataset_id: str, tenant_id: str, tags: list[str]):
    """
    Delete tags from a dataset.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :param tags: list of tags to delete
    :return: (success, result) or (success, error_message)
    """
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'

    if not KnowledgebaseService.accessible(dataset_id, tenant_id):
        return False, "no authorization"

    ok, kb = KnowledgebaseService.get_by_id(dataset_id)
    if not ok:
        return False, "Invalid Dataset ID"

    from rag.nlp import search

    for t in tags:
        settings.docStoreConn.update({"tag_kwd": t, "kb_id": [dataset_id]}, {"remove": {"tag_kwd": t}}, search.index_name(kb.tenant_id), dataset_id)

    return True, {}


def run_embedding(dataset_id: str, tenant_id: str):
    """
    Run embedding for all documents in a dataset.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :return: (success, result) or (success, error_message)
    """
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'

    if not KnowledgebaseService.accessible(dataset_id, tenant_id):
        return False, "No authorization."

    ok, kb = KnowledgebaseService.get_by_id(dataset_id)
    if not ok:
        return False, "Invalid Dataset ID"

    documents, _ = DocumentService.get_by_kb_id(
        kb_id=dataset_id,
        page_number=0,
        items_per_page=0,
        orderby="create_time",
        desc=False,
        keywords="",
        run_status=[],
        types=[],
        suffix=[],
    )
    if not documents:
        return False, f"No documents in Dataset {dataset_id}"

    try:
        DocumentService.assert_documents_docmind_evidence_mutable(
            [document["id"] for document in documents],
            "REBUILD_DOCUMENT_EMBEDDINGS",
            actor_id=tenant_id,
        )
    except DocmindProtectedEvidenceError as error:
        return False, error.code

    kb_table_num_map = {}
    for doc in documents:
        doc["tenant_id"] = tenant_id
        DocumentService.run(tenant_id, doc, kb_table_num_map)

    return True, {"scheduled_count": len(documents)}


def rename_tag(dataset_id: str, tenant_id: str, from_tag: str, to_tag: str):
    """
    Rename a tag in a dataset.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :param from_tag: original tag name
    :param to_tag: new tag name
    :return: (success, result) or (success, error_message)
    """
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'

    if not KnowledgebaseService.accessible(dataset_id, tenant_id):
        return False, "no authorization"

    ok, kb = KnowledgebaseService.get_by_id(dataset_id)
    if not ok:
        return False, "Invalid Dataset ID"

    from rag.nlp import search

    settings.docStoreConn.update(
        {"tag_kwd": from_tag, "kb_id": [dataset_id]}, {"remove": {"tag_kwd": from_tag.strip()}, "add": {"tag_kwd": to_tag}}, search.index_name(kb.tenant_id), dataset_id
    )

    return True, {"from": from_tag, "to": to_tag}


async def search(dataset_id: str, tenant_id: str, req: dict):
    """
    Search (retrieval test) within a dataset.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :param req: search request
    :return: (success, result) or (success, error_message)
    """
    from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type
    from api.db.services.doc_metadata_service import DocMetadataService
    from api.db.services.llm_service import LLMBundle
    from api.db.services.user_service import UserTenantService
    from common.constants import LLMType
    from common.metadata_utils import apply_meta_data_filter
    from rag.app.tag import label_question
    from rag.prompts.generator import cross_languages, keyword_extraction

    logging.debug(
        "search(dataset=%s, tenant=%s, question_len=%s)",
        dataset_id,
        tenant_id,
        len(req.get("question", "")),
    )

    page = int(req.get("page", 1))
    size = int(req.get("page_size") or req.get("size", 30))
    rerank_candidates_count = int(req.get("rerank_candidates_count", 64))
    question = req.get("question", "")
    doc_ids = req.get("doc_ids", [])
    similarity_threshold = float(req.get("similarity_threshold", 0.0))
    vector_similarity_weight = float(req.get("vector_similarity_weight", 0.3))
    knn_top_k = max(1, min(int(req.get("knn_top_k", 1024)), 2048))
    knn_num_candidates = int(req.get("knn_num_candidates", 2048))
    langs = req.get("cross_languages", [])

    if not KnowledgebaseService.accessible(dataset_id, tenant_id):
        logging.warning("search access denied: dataset=%s tenant=%s", dataset_id, tenant_id)
        return False, "Only owner of dataset authorized for this operation."

    e, kb = KnowledgebaseService.get_by_id(dataset_id)
    if not e:
        logging.warning("search dataset not found: dataset=%s", dataset_id)
        return False, "Dataset not found!"

    if doc_ids is not None and not isinstance(doc_ids, list):
        return False, "`doc_ids` should be a list"
    local_doc_ids = list(doc_ids) if doc_ids else []

    meta_data_filter = req.get("meta_data_filter") or {}
    search_config = {}
    chat_mdl = None
    if meta_data_filter.get("method") in ["auto", "semi_auto"]:
        chat_model_config = get_tenant_default_model_by_type(tenant_id, LLMType.CHAT)
        chat_mdl = LLMBundle(tenant_id, chat_model_config)

    if meta_data_filter:
        local_doc_ids = await apply_meta_data_filter(
            meta_data_filter,
            None,
            question,
            chat_mdl,
            local_doc_ids,
            kb_ids=[dataset_id],
            metas_loader=lambda: DocMetadataService.get_flatted_meta_by_kbs([dataset_id]),
        )

    tenant_ids = []
    tenants = UserTenantService.query(user_id=tenant_id)
    for tenant in tenants:
        if KnowledgebaseService.query(tenant_id=tenant.tenant_id, id=dataset_id):
            tenant_ids.append(tenant.tenant_id)
            break
    else:
        return False, "Only owner of dataset authorized for this operation."

    _question = question
    if langs:
        _question = await cross_languages(kb.tenant_id, None, _question, langs)
    if kb.embd_id:
        embd_model_config = resolve_model_config(kb.tenant_id, LLMType.EMBEDDING, kb.embd_id)
    else:
        embd_model_config = get_tenant_default_model_by_type(kb.tenant_id, LLMType.EMBEDDING)
    embd_mdl = LLMBundle(kb.tenant_id, embd_model_config)

    rerank_mdl = None
    rerank_id = req.get("rerank_id") or search_config.get("rerank_id")
    rerank_candidate_limit = req.get("rerank_candidate_limit")
    if rerank_id:
        rerank_model_config = resolve_model_config(kb.tenant_id, LLMType.RERANK.value, rerank_id)
        rerank_mdl = LLMBundle(kb.tenant_id, rerank_model_config)
    elif rerank_candidate_limit is not None:
        return False, "rerank_candidate_limit requires rerank_id"

    if search_config.get("keyword", req.get("keyword", False)):
        default_chat_model_config = get_tenant_default_model_by_type(kb.tenant_id, LLMType.CHAT)
        chat_mdl = LLMBundle(kb.tenant_id, default_chat_model_config)
        _question += await keyword_extraction(chat_mdl, _question)

    labels = label_question(_question, [kb])
    ranks = await settings.retriever.retrieval(
        _question,
        embd_mdl,
        tenant_ids,
        [dataset_id],
        page,
        size,
        similarity_threshold,
        vector_similarity_weight,
        doc_ids=local_doc_ids,
        knn_top_k=knn_top_k,
        knn_num_candidates=knn_num_candidates,
        rerank_mdl=rerank_mdl,
        rerank_candidate_limit=rerank_candidate_limit,
        rank_feature=labels,
        trace_id="",
        must_not={"exists": "compile_kwd"},
        rerank_candidates_count=rerank_candidates_count,
    )

    ranks["chunks"] = settings.retriever.retrieval_by_children(ranks["chunks"], tenant_ids)
    ranks["total"] = len(ranks["chunks"])

    for c in ranks["chunks"]:
        c.pop("vector", None)
    ranks["labels"] = labels

    return True, ranks


def check_embedding(dataset_id: str, tenant_id: str, req: dict):
    """
    Check embedding model compatibility by sampling random chunks,
    re-embedding them with the new model, and computing cosine similarity.

    :param dataset_id: dataset ID
    :param tenant_id: tenant ID
    :param req: request body with embd_id
    :return: (success, result) or (success, error_message)
    """
    import random

    import numpy as np

    from api.db.services.llm_service import LLMBundle
    from common.constants import LLMType, RetCode
    from common.doc_store.doc_store_base import OrderByExpr
    from rag.nlp import search

    def _guess_vec_field(src: dict):
        for k in src or {}:
            if k.endswith("_vec"):
                return k
        return None

    def _as_float_vec(v):
        if v is None:
            return []
        if isinstance(v, str):
            return [float(x) for x in v.split("\t") if x != ""]
        if isinstance(v, (list, tuple, np.ndarray)):
            return [float(x) for x in v]
        return []

    def _to_1d(x):
        a = np.asarray(x, dtype=np.float32)
        return a.reshape(-1)

    def _cos_sim(a, b, eps=1e-12):
        a = _to_1d(a)
        b = _to_1d(b)
        na = np.linalg.norm(a)
        nb = np.linalg.norm(b)
        if na < eps or nb < eps:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    def sample_random_chunks_with_vectors(
        docStoreConn,
        tenant_id: str,
        kb_id: str,
        n: int = 5,
        base_fields=("docnm_kwd", "doc_id", "content_with_weight", "page_num_int", "position_int", "top_int"),
    ):
        index_nm = search.index_name(tenant_id)
        try:
            res0 = docStoreConn.search(
                select_fields=[],
                highlight_fields=[],
                condition={"kb_id": kb_id, "available_int": 1},
                match_expressions=[],
                order_by=OrderByExpr(),
                offset=0,
                limit=1,
                index_names=index_nm,
                knowledgebase_ids=[kb_id],
            )
        except Exception as e:
            if "not_found_exception" in repr(e) or "index_not_found_exception" in repr(e):
                logging.info(
                    "sample_random_chunks_with_vectors: index %s not yet created for tenant %s; returning empty sample set",
                    index_nm,
                    tenant_id,
                )
                return []
            raise
        total = docStoreConn.get_total(res0)
        if total <= 0:
            return []

        n = min(n, total)
        offsets = sorted(random.sample(range(min(total, 1000)), n))
        out = []

        for off in offsets:
            res1 = docStoreConn.search(
                select_fields=list(base_fields),
                highlight_fields=[],
                condition={"kb_id": kb_id, "available_int": 1},
                match_expressions=[],
                order_by=OrderByExpr(),
                offset=off,
                limit=1,
                index_names=index_nm,
                knowledgebase_ids=[kb_id],
            )
            ids = docStoreConn.get_doc_ids(res1)
            if not ids:
                continue

            cid = ids[0]
            full_doc = docStoreConn.get(cid, index_nm, [kb_id]) or {}
            vec_field = _guess_vec_field(full_doc)
            vec = _as_float_vec(full_doc.get(vec_field))

            out.append(
                {
                    "chunk_id": cid,
                    "kb_id": kb_id,
                    "doc_id": full_doc.get("doc_id"),
                    "doc_name": full_doc.get("docnm_kwd"),
                    "vector_field": vec_field,
                    "vector_dim": len(vec),
                    "vector": vec,
                    "page_num_int": full_doc.get("page_num_int"),
                    "position_int": full_doc.get("position_int"),
                    "top_int": full_doc.get("top_int"),
                    "content_with_weight": full_doc.get("content_with_weight") or "",
                    "question_kwd": full_doc.get("question_kwd") or [],
                }
            )
        return out

    def _clean(s: str):
        return re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", s or "").strip()

    if not dataset_id:
        return False, 'Lack of "Dataset ID"'

    if not KnowledgebaseService.accessible(dataset_id, tenant_id):
        return False, "no authorization"

    ok, kb = KnowledgebaseService.get_by_id(dataset_id)
    if not ok:
        return False, "Invalid Dataset ID"

    embd_id = req.get("embd_id", "")
    if not embd_id:
        return False, "`embd_id` is required."

    logging.info("check_embedding: dataset=%s tenant=%s embd_id=%s", dataset_id, tenant_id, embd_id)

    ok, err = verify_embedding_availability(embd_id, tenant_id)
    if not ok:
        return False, err

    embd_model_config = resolve_model_config(kb.tenant_id, LLMType.EMBEDDING, embd_id)
    emb_mdl = LLMBundle(kb.tenant_id, embd_model_config)

    n = int(req.get("check_num", 5))
    samples = sample_random_chunks_with_vectors(settings.docStoreConn, tenant_id=kb.tenant_id, kb_id=dataset_id, n=n)
    logging.info("check_embedding: dataset=%s sampled=%d chunks", dataset_id, len(samples))

    results, eff_sims = [], []
    mode = "content_only"
    for ck in samples:
        title = ck.get("doc_name") or "Title"

        txt_in = "\n".join(ck.get("question_kwd") or []) or ck.get("content_with_weight") or ""
        txt_in = _clean(txt_in)
        if not txt_in:
            results.append({"chunk_id": ck["chunk_id"], "reason": "no_text"})
            continue

        if not ck.get("vector"):
            results.append({"chunk_id": ck["chunk_id"], "reason": "no_stored_vector"})
            continue

        try:
            v, _ = emb_mdl.encode([title, txt_in])
            assert len(v[1]) == len(ck["vector"]), f"The dimension ({len(v[1])}) of given embedding model is different from the original ({len(ck['vector'])})"
            sim_content = _cos_sim(v[1], ck["vector"])
            title_w = 0.1
            qv_mix = title_w * v[0] + (1 - title_w) * v[1]
            sim_mix = _cos_sim(qv_mix, ck["vector"])
            sim = sim_content
            mode = "content_only"
            if sim_mix > sim:
                sim = sim_mix
                mode = "title+content"
        except Exception as e:
            return False, f"Embedding failure. {e}"

        eff_sims.append(sim)
        results.append(
            {
                "chunk_id": ck["chunk_id"],
                "doc_id": ck["doc_id"],
                "doc_name": ck["doc_name"],
                "vector_field": ck["vector_field"],
                "vector_dim": ck["vector_dim"],
                "cos_sim": round(sim, 6),
            }
        )

    summary = {
        "kb_id": dataset_id,
        "model": embd_id,
        "sampled": len(samples),
        "valid": len(eff_sims),
        "avg_cos_sim": round(float(np.mean(eff_sims)) if eff_sims else 0.0, 6),
        "min_cos_sim": round(float(np.min(eff_sims)) if eff_sims else 0.0, 6),
        "max_cos_sim": round(float(np.max(eff_sims)) if eff_sims else 0.0, 6),
        "match_mode": mode,
    }

    data = {"summary": summary, "results": results}
    if not eff_sims:
        logging.warning("check_embedding: dataset=%s no comparable chunks", dataset_id)
        return False, "No embedded chunks are available to compare."
    if summary["avg_cos_sim"] >= 0.9:
        logging.info("check_embedding: dataset=%s compatible avg_cos_sim=%s valid=%d", dataset_id, summary["avg_cos_sim"], len(eff_sims))
        return True, data
    logging.warning("check_embedding: dataset=%s not_effective avg_cos_sim=%s valid=%d", dataset_id, summary["avg_cos_sim"], len(eff_sims))
    return "not_effective", {
        "code": RetCode.NOT_EFFECTIVE,
        "message": "Embedding model switch failed: the average similarity between old and new vectors is below 0.9, indicating incompatible vector spaces.",
        "data": data,
    }


async def search_datasets(tenant_id: str, req: dict, *, candidate_mode: str = "hybrid", body_terms: str = ""):
    """
    Search (retrieval test) across multiple datasets.

    :param tenant_id: tenant ID
    :param req: search request containing dataset_ids and other params
    :return: (success, result) or (success, error_message)
    """
    from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type
    from api.db.services.doc_metadata_service import DocMetadataService
    from api.db.services.llm_service import LLMBundle
    from api.db.services.user_service import UserTenantService
    from common.constants import LLMType
    from common.metadata_utils import apply_meta_data_filter
    from rag.app.tag import label_question
    from rag.prompts.generator import cross_languages, keyword_extraction

    if candidate_mode not in {"hybrid", "dense", "bm25"}:
        return False, "candidate_mode must be 'hybrid', 'dense', or 'bm25'"

    kb_ids = req.get("dataset_ids", [])
    page = int(req.get("page", 1))
    size = int(req.get("page_size") or req.get("size", 30))
    rerank_candidates_count = int(req.get("rerank_candidates_count", 64))
    question = req.get("question", "")
    doc_ids = req.get("doc_ids", [])
    similarity_threshold = float(req.get("similarity_threshold", 0.0))
    vector_similarity_weight = float(req.get("vector_similarity_weight", 0.3))
    knn_top_k = max(1, min(int(req.get("knn_top_k", 1024)), 2048))
    knn_num_candidates = int(req.get("knn_num_candidates", 2048))
    candidate_vector_similarity_weight = req.get("candidate_vector_similarity_weight", 0.95)
    if isinstance(candidate_vector_similarity_weight, bool):
        return False, "candidate_vector_similarity_weight must be a finite number in [0, 1]"
    try:
        candidate_vector_similarity_weight = float(candidate_vector_similarity_weight)
    except (TypeError, ValueError):
        return False, "candidate_vector_similarity_weight must be a finite number in [0, 1]"
    if not math.isfinite(candidate_vector_similarity_weight) or not 0 <= candidate_vector_similarity_weight <= 1:
        return False, "candidate_vector_similarity_weight must be a finite number in [0, 1]"
    langs = req.get("cross_languages", [])

    logging.debug(
        "search_datasets(datasets=%s, tenant=%s, question_len=%s)",
        kb_ids,
        tenant_id,
        len(question),
    )

    # Access check for all datasets
    for kb_id in kb_ids:
        if not KnowledgebaseService.accessible(kb_id, tenant_id):
            logging.warning("search_datasets access denied: dataset=%s tenant=%s", kb_id, tenant_id)
            return False, f"Only owner of dataset {kb_id} authorized for this operation."

    kbs = KnowledgebaseService.get_by_ids(kb_ids)
    if not kbs:
        return False, "Datasets not found!"

    err = validate_dataset_embedding_models(kbs)
    if err:
        return False, err

    if doc_ids is not None and not isinstance(doc_ids, list):
        return False, "`doc_ids` should be a list"
    local_doc_ids = list(doc_ids) if doc_ids else []

    meta_data_filter = req.get("meta_data_filter") or {}
    search_config = {}
    chat_mdl = None
    if meta_data_filter.get("method") in ["auto", "semi_auto"]:
        chat_model_config = get_tenant_default_model_by_type(tenant_id, LLMType.CHAT)
        chat_mdl = LLMBundle(tenant_id, chat_model_config)

    if meta_data_filter:
        logging.debug("Metadata filter applied: %s, question length: %d, chat_mdl=%s", meta_data_filter, len(question), "None" if chat_mdl is None else "configured")
        local_doc_ids = await apply_meta_data_filter(
            meta_data_filter,
            None,
            question,
            chat_mdl,
            local_doc_ids,
            kb_ids=kb_ids,
            metas_loader=lambda: DocMetadataService.get_flatted_meta_by_kbs(kb_ids),
        )

    tenant_ids = []
    tenants = UserTenantService.query(user_id=tenant_id)
    for tenant in tenants:
        if any(KnowledgebaseService.query(tenant_id=tenant.tenant_id, id=kb_id) for kb_id in kb_ids):
            tenant_ids.append(tenant.tenant_id)
            break
    else:
        return False, "Only owner of datasets authorized for this operation."

    kb = kbs[0]
    _question = question
    if langs:
        _question = await cross_languages(kb.tenant_id, None, _question, langs)

    embd_mdl = None
    if kb.embd_id:
        embd_model_config = resolve_model_config(kb.tenant_id, LLMType.EMBEDDING, kb.embd_id)
    else:
        embd_model_config = get_tenant_default_model_by_type(kb.tenant_id, LLMType.EMBEDDING)
    embd_mdl = LLMBundle(kb.tenant_id, embd_model_config)

    rerank_mdl = None
    rerank_id = req.get("rerank_id") or search_config.get("rerank_id")
    rerank_candidate_limit = req.get("rerank_candidate_limit")
    if rerank_id:
        rerank_model_config = resolve_model_config(kb.tenant_id, LLMType.RERANK.value, rerank_id)
        rerank_mdl = LLMBundle(kb.tenant_id, rerank_model_config)
    elif rerank_candidate_limit is not None:
        return False, "rerank_candidate_limit requires rerank_id"

    if search_config.get("keyword", req.get("keyword", False)):
        default_chat_model_config = get_tenant_default_model_by_type(kb.tenant_id, LLMType.CHAT)
        chat_mdl = LLMBundle(kb.tenant_id, default_chat_model_config)
        _question += await keyword_extraction(chat_mdl, _question)

    labels = label_question(_question, kbs)
    ranks = await settings.retriever.retrieval(
        _question,
        embd_mdl,
        tenant_ids,
        kb_ids,
        page,
        size,
        similarity_threshold,
        vector_similarity_weight,
        doc_ids=local_doc_ids,
        knn_top_k=knn_top_k,
        knn_num_candidates=knn_num_candidates,
        rerank_mdl=rerank_mdl,
        rerank_candidate_limit=rerank_candidate_limit,
        rank_feature=labels,
        trace_id="",
        must_not={"exists": "compile_kwd"},
        rerank_candidates_count=rerank_candidates_count,
        candidate_mode=candidate_mode,
        candidate_vector_similarity_weight=candidate_vector_similarity_weight,
        **({"body_terms": body_terms} if body_terms else {}),
    )

    if candidate_mode == "hybrid":
        ranks["chunks"] = settings.retriever.retrieval_by_children(ranks["chunks"], tenant_ids)
    else:
        # DocMind's independent BM25/dense lanes must preserve datastore rank
        # positions for RRF. Parent-to-child expansion can reorder and enlarge
        # a lane, so it belongs only to the legacy hybrid retrieval path.
        ranks["chunks"] = ranks["chunks"][:size]

    for c in ranks["chunks"]:
        c.pop("vector", None)
    ranks["labels"] = labels

    return True, ranks
