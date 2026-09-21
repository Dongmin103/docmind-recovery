import asyncio
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from peewee import SqliteDatabase

from api.apps.services import docmind_generation_service as service
from api.db.db_models import (
    DocmindAuditEvent,
    DocmindCatalogVersion,
    DocmindDocumentRoutingDigest,
    DocmindDraftChange,
    DocmindFolder,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindIdempotencyOperation,
    DocmindProject,
    DocmindRegistration,
    DocmindRegistrationCurrent,
    Document,
)
from api.db.services import docmind_catalog_service


MODELS = [
    DocmindProject,
    DocmindFolder,
    DocmindRegistration,
    DocmindRegistrationCurrent,
    DocmindDocumentRoutingDigest,
    DocmindCatalogVersion,
    DocmindFolderVersion,
    DocmindFolderVersionDocument,
    DocmindDraftChange,
    DocmindIdempotencyOperation,
    DocmindAuditEvent,
    Document,
]


def _capability():
    root = {
        "root_uri": "viking://resources/a/",
        "entry_count": 9,
        "identity_sha256": "a" * 64,
    }
    return {
        "schema": "docmind-train-e-openviking-capability-v1",
        "status": "PASS",
        "processing_contract": {
            "processing_mode": "semantic_and_vectors",
            "source_name": "manifest.md",
            "strict": True,
            "telemetry": False,
            "wait": True,
            "watch_interval": 0,
        },
        "runtime_before": {"image_id": "image-1"},
        "runtime_after_restart": {"image_id": "image-1"},
        "first_before_restart": root,
        "first_after_restart": root,
        "second_root": {**root, "root_uri": "viking://resources/b/"},
        "missing_root_detected": True,
        "active_catalog_or_serving_root_changed": False,
    }


def test_capability_artifact_is_exact_and_hash_bound(tmp_path):
    path = tmp_path / "capability.json"
    path.write_text(json.dumps(_capability()))

    result = service.validate_capability_artifact(path)

    assert result["image_id"] == "image-1"
    assert len(result["sha256"]) == 64
    broken = _capability()
    broken["active_catalog_or_serving_root_changed"] = True
    path.write_text(json.dumps(broken))
    with pytest.raises(service.DocmindGenerationError, match="CAPABILITY_FAILED"):
        service.validate_capability_artifact(path)


def test_generator_model_uses_docmind_override_without_changing_tenant_default(monkeypatch):
    observed = {}

    def configured(tenant_id, model_type, model_name):
        observed.update(
            tenant_id=tenant_id,
            model_type=model_type,
            model_name=model_name,
        )
        return {
            "llm_name": "qwen3.6-plus",
            "llm_factory": "Tongyi-Qianwen",
        }

    monkeypatch.setenv(
        service.GENERATOR_MODEL_ENV,
        "qwen3.6-plus@QwenCloud@Tongyi-Qianwen",
    )
    monkeypatch.setattr(service, "get_model_config_from_provider_instance", configured)
    monkeypatch.setattr(
        service,
        "get_tenant_default_model_by_type",
        lambda *_args: pytest.fail("tenant default model changed"),
    )

    result = service._generator_model_config("tenant-1")

    assert result["llm_name"] == "qwen3.6-plus"
    assert observed == {
        "tenant_id": "tenant-1",
        "model_type": service.LLMType.CHAT,
        "model_name": "qwen3.6-plus@QwenCloud@Tongyi-Qianwen",
    }


def test_generator_model_falls_back_to_tenant_default_without_override(monkeypatch):
    monkeypatch.delenv(service.GENERATOR_MODEL_ENV, raising=False)
    monkeypatch.setattr(
        service,
        "get_tenant_default_model_by_type",
        lambda tenant_id, model_type: {
            "tenant_id": tenant_id,
            "model_type": model_type,
            "llm_name": "default-chat",
        },
    )

    result = service._generator_model_config("tenant-1")

    assert result["llm_name"] == "default-chat"


def test_generator_model_fails_closed_when_override_is_unavailable(monkeypatch):
    monkeypatch.setenv(service.GENERATOR_MODEL_ENV, "missing@instance@provider")
    monkeypatch.setattr(
        service,
        "get_model_config_from_provider_instance",
        lambda *_args: (_ for _ in ()).throw(LookupError("missing")),
    )

    with pytest.raises(
        service.DocmindGenerationError,
        match="DOCMIND_GENERATOR_MODEL_UNAVAILABLE",
    ):
        service._generator_model_config("tenant-1")


def test_routing_digest_is_deterministic_and_bounded():
    chunks = [
        {"id": "b", "content_with_weight": "Validation protocol qualification acceptance criteria"},
        {"id": "a", "content_with_weight": "Process validation lifecycle and continued verification"},
    ]

    first, first_fingerprint = service.build_routing_digest(
        document_name="Validation.pdf",
        chunks=chunks,
    )
    second, second_fingerprint = service.build_routing_digest(
        document_name="Validation.pdf",
        chunks=list(reversed(chunks)),
    )

    assert first == second
    assert first_fingerprint == second_fingerprint
    assert first["schema"] == "docmind-routing-digest-v1"
    assert first["chunk_count"] == 2
    assert "validation" in first["keywords"]
    assert len(first["representative_excerpts"]) == 2


def test_hierarchy_prompt_bounds_large_folder_and_preserves_full_folder_signals():
    folder = SimpleNamespace(
        folder_id="folder-root",
        parent_folder_id=None,
        display_name="AI Reference",
        relative_path="ai-ref",
        l0_text=None,
        l1_text=None,
    )
    memberships = [
        SimpleNamespace(folder_id="folder-root", document_id=f"doc-{index:04d}")
        for index in range(1000)
    ]
    digests = {
        membership.document_id: {
            "document_name": f"Document {index:04d}.pdf",
            "keywords": ["shared-signal", f"topic-{index:04d}"] + ["detail"] * 20,
            "representative_excerpts": ["x" * 1000, "y" * 1000],
        }
        for index, membership in enumerate(memberships)
    }

    _system, first_user = service._hierarchy_prompt(
        [folder], memberships, digests, [folder.folder_id]
    )
    _system, second_user = service._hierarchy_prompt(
        [folder], list(reversed(memberships)), digests, [folder.folder_id]
    )

    assert first_user == second_user
    payload = json.loads(first_user)
    tree = payload["tree"][0]
    assert tree["direct_document_count"] == 1000
    assert "shared-signal" in tree["aggregate_keywords"]
    assert len(tree["aggregate_keywords"]) <= service.HIERARCHY_AGGREGATE_KEYWORD_LIMIT
    assert (
        len(tree["representative_documents"])
        == service.HIERARCHY_DOCUMENT_SAMPLE_LIMIT
    )
    assert all(
        len(row["keywords"]) <= service.HIERARCHY_DOCUMENT_KEYWORD_LIMIT
        for row in tree["representative_documents"]
    )
    assert all(
        len(row["representative_excerpts"])
        <= service.HIERARCHY_DOCUMENT_EXCERPT_LIMIT
        for row in tree["representative_documents"]
    )
    assert len(first_user) < 30000


def test_card_output_requires_exact_five_contrastive_cards():
    folder_ids = ["a", "b", "c", "d", "e"]
    raw = {
        "folders": [
            {
                "id": folder_id,
                "l0": f"{folder_id} 폴더의 담당 범위, 대표적인 긍정 신호, 다른 폴더와 구분되는 명확한 경계를 설명합니다.",
                "l1": (
                    f"{folder_id}의 긍정 신호, 제외 조건, 이웃 폴더 비교, 공동 라우팅 규칙, "
                    "그리고 대표 문서군을 설명합니다. " * 3
                ),
            }
            for folder_id in folder_ids
        ]
    }

    normalized = service.normalize_generated_cards(raw, folder_ids)

    assert list(normalized) == folder_ids
    assert "Routing responsibility" in service._folder_manifest(
        "Folder A",
        normalized["a"]["l0"],
        normalized["a"]["l1"],
    )
    with pytest.raises(service.DocmindGenerationError):
        service.normalize_generated_cards(
            {"folders": raw["folders"][:-1]},
            folder_ids,
        )


def test_openviking_placeholder_sidecar_fails_before_router_validation():
    with pytest.raises(
        service.DocmindGenerationError,
        match="DOCMIND_OPENVIKING_SIDECAR_GENERATION_FAILED",
    ):
        service._validate_generated_sidecar(
            "[Directory overview is not generated]",
            "# validation\n\n[Directory overview is not generated]",
        )

    service._validate_generated_sidecar(
        "밸리데이션(Validation) 수명주기의 담당 범위와 구분 경계입니다.",
        "상세한 긍정 신호와 제외 조건 및 이웃 폴더와의 비교를 설명합니다.",
    )

    with pytest.raises(
        service.DocmindGenerationError,
        match="DOCMIND_OPENVIKING_SIDECAR_LANGUAGE_INVALID",
    ):
        service._validate_generated_sidecar(
            "Validation lifecycle responsibilities and boundaries.",
            "Detailed positive signals, exclusions, and sibling comparisons.",
        )


def test_staging_root_404_is_absent_but_other_errors_fail(monkeypatch):
    client = object.__new__(service.OpenVikingStagingClient)
    client.base_url = "http://openviking"
    client.headers = {"X-API-Key": "redacted"}

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: Response(404, {"status": "error"}),
    )
    assert client.root_exists("viking://resources/new/") is False
    monkeypatch.setattr(
        service.requests,
        "get",
        lambda *args, **kwargs: Response(500, {"status": "error"}),
    )
    with pytest.raises(service.DocmindGenerationError, match="READ_FAILED"):
        client.root_exists("viking://resources/new/")


def test_staging_root_delete_is_recursive_async_and_404_idempotent(monkeypatch):
    client = object.__new__(service.OpenVikingStagingClient)
    client.base_url = "http://openviking"
    client.headers = {"X-API-Key": "redacted"}
    observed = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def delete(*args, **kwargs):
        observed.append((args, kwargs))
        return Response(
            200,
            {
                "status": "ok",
                "result": {"uri": "viking://resources/docmind-catalog-failed/"},
            },
        )

    monkeypatch.setattr(service.requests, "delete", delete)
    assert client.delete_root("viking://resources/docmind-catalog-failed/") is True
    assert observed[0][1]["params"] == {
        "uri": "viking://resources/docmind-catalog-failed/",
        "recursive": "true",
        "wait": "false",
    }
    assert observed[0][1]["timeout"] == 30
    monkeypatch.setattr(
        service.requests,
        "delete",
        lambda *args, **kwargs: Response(404, {"status": "error"}),
    )
    assert client.delete_root("viking://resources/docmind-catalog-failed/") is False


def test_hierarchical_ovpack_preserves_nested_folder_paths_without_file_directory_collision():
    pack = service.OpenVikingStagingClient._manual_ovpack(
        "hierarchy-root",
        [
            {"folder_id": "GMP", "l0": "한국어 L0", "l1": "한국어 L1"},
            {
                "folder_id": "GMP/Validation/Cleaning",
                "l0": "세척 한국어 L0",
                "l1": "세척 한국어 L1",
            },
        ],
    )

    with zipfile.ZipFile(io.BytesIO(pack)) as archive:
        names = archive.namelist()
        assert (
            "hierarchy-root/files/GMP/Validation/Cleaning/.abstract.md"
            in names
        )
        assert (
            "hierarchy-root/files/GMP/Validation/Cleaning/manifest.md/.overview.md"
            in names
        )
        assert "hierarchy-root/files/GMP/Validation/Cleaning/manifest.md" not in names


def test_card_prompt_uses_safe_digest_projection_without_raw_excerpts():
    folders = [SimpleNamespace(id="folder-row", slug="validation", display_name="Validation")]
    memberships = [SimpleNamespace(folder_id="folder-row", document_id="doc-new")]
    digests = {
        "doc-new": {
            "document_name": "Validation Guide.pdf",
            "chunk_count": 160,
            "keywords": ["validation", "qualification"],
            "representative_excerpts": ["raw regulated source passage must not be sent"],
        }
    }

    system, user = service._card_prompt(
        folders,
        memberships,
        digests,
        {"validation": {"l0": "current l0", "l1": "current l1"}},
        {"doc-new"},
    )

    assert "raw regulated source passage" not in user
    assert '"change":"ADD"' in user
    assert '"keywords":["validation","qualification"]' in user
    assert "모든 설명 문장은 한국어로 작성" in system
    assert "영문을 괄호로 병기" in system


def _validation_rows():
    rows = []
    for index in range(80):
        negative = index >= 75
        acceptable = [] if negative else (["a", "b"] if index < 6 else ["a"])
        rows.append(
            {
                "id": f"row-{index:02d}",
                "question": f"question-{index:02d}",
                "acceptable_folder_ids": acceptable,
                "class": "negative_boundary_diagnostic" if negative else "positive",
            }
        )
    return rows


def test_router_validation_enforces_fixed_denominators_and_thresholds(monkeypatch):
    monkeypatch.setattr(
        service.docmind_api_service,
        "_find_folders",
        lambda question, catalog, trace_id: [
            {"id": "a"},
            {"id": "b"},
            {"id": "c"},
        ],
    )

    report = asyncio.run(
        service._router_validation(
            SimpleNamespace(),
            _validation_rows(),
        )
    )

    assert report["row_count"] == 80
    assert report["positive_count"] == 75
    assert report["selected_any_at_3"] == 75
    assert report["selected_all_at_3"] == 75
    assert report["multi_all_at_3"] == 6


def test_default_publish_validation_is_document_scoped_without_full_router_suite():
    assert service._document_scoped_router_report() == {
        "performed": False,
        "scope": "new_document_probe_only",
        "development_question_count": 0,
    }


@pytest.fixture
def generation_db(monkeypatch):
    database = SqliteDatabase(":memory:")
    database.bind(MODELS)
    database.create_tables(MODELS)
    catalog = SimpleNamespace(
        dataset_id="dataset-1",
        root_uri="viking://resources/current/",
        folders={
            "quality-risk-management": ("doc-1",),
            "validation": ("doc-2",),
            "analytical-quality-control": ("doc-3",),
            "biopharmaceutical-manufacturing": ("doc-4",),
            "quality-operations": ("doc-5",),
        },
    )
    loaded = docmind_catalog_service.import_static_v0("tenant-1", catalog)
    project = DocmindProject.get_by_id(loaded.project_id)
    context = SimpleNamespace(project=project, catalog=catalog)
    monkeypatch.setattr(service.docmind_draft_service, "_context", lambda tenant_id: context)
    monkeypatch.setattr(service.docmind_draft_service, "_document_name", lambda document_id: f"{document_id}.pdf")
    monkeypatch.setattr(service, "validate_capability_artifact", lambda: {"sha256": "a" * 64})
    monkeypatch.setattr(
        service,
        "generation_preflight",
        lambda tenant_id, draft_id: {"preflight_sha256": "b" * 64},
    )
    draft = service.docmind_draft_service.create_draft(
        "tenant-1",
        loaded.active_version_id,
        "draft-key",
    )
    DocmindDraftChange.create(
        id="change-1",
        draft_version_id=draft["draft_id"],
        operation="ADD",
        document_id="doc-new",
        registration_id="registration-new",
        from_folder_id=None,
        to_folder_id=DocmindFolder.get(DocmindFolder.slug == "validation").id,
        expected_parent_folder_id=None,
        expected_parent_membership_hash=None,
        actor_id="tenant-1",
        ordinal=0,
        **docmind_catalog_service._timestamps(),
    )
    yield SimpleNamespace(database=database, context=context, draft_id=draft["draft_id"])
    database.drop_tables(list(reversed(MODELS)))
    database.close()


def test_generation_start_is_single_transition_and_spawns_runner(generation_db, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(service, "GENERATOR_LOG_ROOT", tmp_path)
    monkeypatch.setattr(service, "GENERATOR_SCRIPT", tmp_path / "runner.py")
    monkeypatch.setattr(service.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(pid=1))

    result = service.start_generation("tenant-1", generation_db.draft_id)

    assert result["lifecycle_state"] == "GENERATING"
    assert result["generation_progress"] >= 5
    assert len(calls) == 1
    assert calls[0][0][-2:] == ["--draft-id", generation_db.draft_id]
    with pytest.raises(service.DocmindGenerationError, match="STATE_INVALID"):
        service.start_generation("tenant-1", generation_db.draft_id)


def test_failed_validation_resume_is_restricted_to_complete_internal_failure(generation_db):
    service.DocmindCatalogVersion.update(
        lifecycle_state="FAILED",
        health_state="INVALID",
        health_reason="DOCMIND_ROUTER_REGRESSION",
    ).where(service.DocmindCatalogVersion.id == generation_db.draft_id).execute()

    with pytest.raises(service.DocmindGenerationError, match="RESUME_NOT_ALLOWED"):
        asyncio.run(
            service.resume_failed_staged_validation(
                "tenant-1",
                generation_db.draft_id,
            )
        )


def test_hierarchical_generation_updates_only_affected_cards_without_full_evaluation(
    generation_db,
    monkeypatch,
):
    observed_generations = []
    project = generation_db.context.project
    DocmindProject.update(source_root_file_id="source-root").where(
        DocmindProject.id == project.id
    ).execute()
    project = DocmindProject.get_by_id(project.id)
    generation_db.context.project = project
    version = DocmindCatalogVersion.create(
        id="hierarchy-draft",
        project_id=project.id,
        parent_version_id=project.active_version_id,
        version_label="DRAFT-hierarchy",
        lifecycle_state="GENERATING",
        health_state="UNVALIDATED",
        snapshot_hash=service._hash_json({}),
        snapshot_json="{}",
        root_uri="viking://resources/docmind-catalog-hierarchy-draft/",
        readiness_mode="ADMIN_SAVED",
        source_ready_version_id=project.active_version_id,
        snapshot_schema_version=2,
        source_tree_hash="c" * 64,
        created_by="tenant-1",
        **docmind_catalog_service._timestamps(),
    )
    folder_specs = [
        ("node-root", None, "GMP", "GMP", 0, 0),
        ("node-validation", "node-root", "GMP/Validation", "Validation", 0, 1),
        ("node-cleaning", "node-validation", "GMP/Validation/Cleaning", "Cleaning", 0, 2),
    ]
    for index, (folder_id, parent_id, path, name, ordinal, depth) in enumerate(
        folder_specs,
        start=100,
    ):
        DocmindFolder.create(
            id=folder_id,
            project_id=project.id,
            slug=f"node-{index}",
            display_name=name,
            ordinal=index,
            enabled=True,
            source_file_id=f"source-{folder_id}",
            **docmind_catalog_service._timestamps(),
        )
        DocmindFolderVersion.create(
            id=f"folder-version-{folder_id}",
            version_id=version.id,
            folder_id=folder_id,
            l0_text=None,
            l1_text=None,
            l0_hash=None,
            l1_hash=None,
            generator_metadata={},
            parent_folder_id=parent_id,
            source_file_id=f"source-{folder_id}",
            relative_path=path,
            display_name=name,
            ordinal=ordinal,
            depth=depth,
            **docmind_catalog_service._timestamps(),
        )
    Document.create(
        id="hierarchy-doc",
        kb_id=project.dataset_id,
        parser_id="naive",
        parser_config={},
        source_type="docmind_source",
        type="pdf",
        created_by="tenant-1",
        name="cleaning.pdf",
        location="cleaning.pdf",
        size=10,
        suffix="pdf",
        content_hash="content-hash",
        run="3",
        progress=1.0,
        chunk_num=1,
        status="1",
        **docmind_catalog_service._timestamps(),
    )
    DocmindFolderVersionDocument.create(
        id="hierarchy-membership",
        version_id=version.id,
        folder_id="node-cleaning",
        document_id="hierarchy-doc",
        ordinal=0,
        captured_content_hash="content-hash",
        **docmind_catalog_service._timestamps(),
    )

    class Model:
        async def async_chat(self, _system, messages, generation, **kwargs):
            payload = json.loads(messages[0]["content"])
            folder_id = payload["affected_folder_ids"][0]
            observed_generations.append(
                {
                    "folder_id": folder_id,
                    "generation": dict(generation),
                    "kwargs": dict(kwargs),
                    "context_ids": [row["id"] for row in payload["tree"]],
                    "context_cards": {
                        row["id"]: {
                            "l0": row["current_l0"],
                            "l1": row["current_l1"],
                        }
                        for row in payload["tree"]
                    },
                }
            )
            name = next(
                item[3] for item in folder_specs if item[0] == folder_id
            )
            return json.dumps(
                {
                    "folders": [
                        {
                            "id": folder_id,
                            "l0": (
                                f"{name} 폴더의 핵심 주제와 문서 유형, 출처를 식별하고 "
                                "사용자의 질문을 이 폴더로 보내야 하는 대표 신호와 "
                                "인접 업무 폴더를 선택해야 하는 경계를 한국어로 설명합니다."
                            ),
                            "l1": (
                                (
                                    f"{name} 폴더의 직접 문서와 하위 폴더 범위, 핵심 기관과 제품, 전문 용어와 동의어를 제시합니다. "
                                    "포함 신호와 제외 신호를 구체적으로 나누고 같은 상위 폴더의 이웃과 구별하는 기준을 설명합니다. "
                                    "질문의 의도에 따라 이 폴더를 우선 선택하거나 제외해야 하는 판단 근거를 한국어로 설명합니다. "
                                )
                                * 4
                            ),
                        }
                    ]
                },
                ensure_ascii=False,
            )

    staged_identity = {
        "inventory_sha256": "d" * 64,
        "entry_count": 30,
        "manual_sidecar_set_sha256": "e" * 64,
    }
    staged_identity["identity_sha256"] = service._hash_json(staged_identity)

    class Client:
        def root_exists(self, _root):
            return False

        def import_exact_cards(self, _root_name, cards):
            assert [card["folder_id"] for card in cards] == [
                "GMP",
                "GMP/Validation",
                "GMP/Validation/Cleaning",
            ]
            assert all(
                service.HIERARCHY_L0_LENGTH[0]
                <= len(card["l0"])
                <= service.HIERARCHY_L0_LENGTH[1]
                for card in cards
            )
            assert all(
                service.HIERARCHY_L1_LENGTH[0]
                <= len(card["l1"])
                <= service.HIERARCHY_L1_LENGTH[1]
                for card in cards
            )
            return version.root_uri, staged_identity

    monkeypatch.setattr(service, "LLMBundle", lambda *_args, **_kwargs: Model())
    monkeypatch.setattr(
        service,
        "_generator_model_config",
        lambda _tenant: {"llm_name": "qwen", "llm_factory": "Qwen"},
    )
    monkeypatch.setattr(
        service,
        "_document_chunks",
        lambda *_args: [{"id": "chunk", "content_with_weight": "세척 밸리데이션 절차와 허용 기준"}],
    )
    monkeypatch.setattr(service, "OpenVikingStagingClient", Client)
    monkeypatch.setattr(
        service,
        "_router_validation",
        lambda *_args, **_kwargs: pytest.fail("full router evaluation must not run"),
    )

    result = asyncio.run(
        service.generate_and_validate_draft("tenant-1", version.id)
    )

    assert result["lifecycle_state"] == "READY"
    assert result["folder_count"] == 3
    assert result["affected_count"] == 3
    assert result["search_validation_performed"] is False
    assert [call["folder_id"] for call in observed_generations] == [
        "node-cleaning",
        "node-validation",
        "node-root",
    ]
    validation_context = observed_generations[1]["context_cards"]
    root_context = observed_generations[2]["context_cards"]
    assert validation_context["node-cleaning"]["l0"]
    assert validation_context["node-cleaning"]["l1"]
    assert root_context["node-validation"]["l0"]
    assert root_context["node-validation"]["l1"]
    assert all(
        call["generation"] == service.CARD_GENERATION
        for call in observed_generations
    )
    assert all(
        call["kwargs"] == {"max_tokens": service.CARD_MAX_TOKENS}
        for call in observed_generations
    )
    assert all(
        call["folder_id"] in call["context_ids"]
        for call in observed_generations
    )
    stored = DocmindCatalogVersion.get_by_id(version.id)
    assert stored.snapshot_schema_version == 2
    assert json.loads(stored.snapshot_json)["schema_version"] == 2


def test_hierarchy_move_refreshes_old_and_new_parent_branches(generation_db):
    project = generation_db.context.project
    previous = DocmindCatalogVersion.create(
        id="hierarchy-previous",
        project_id=project.id,
        parent_version_id=project.active_version_id,
        version_label="HIERARCHY-PREVIOUS",
        lifecycle_state="SUPERSEDED",
        health_state="VALID",
        snapshot_hash="a" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/hierarchy-previous/",
        snapshot_schema_version=2,
        created_by="tenant-1",
        **docmind_catalog_service._timestamps(),
    )
    current = DocmindCatalogVersion.create(
        id="hierarchy-current",
        project_id=project.id,
        parent_version_id=previous.id,
        version_label="HIERARCHY-CURRENT",
        lifecycle_state="DRAFT",
        health_state="UNVALIDATED",
        snapshot_hash="b" * 64,
        snapshot_json="{}",
        root_uri="viking://resources/hierarchy-current/",
        snapshot_schema_version=2,
        created_by="tenant-1",
        **docmind_catalog_service._timestamps(),
    )
    nodes = [
        ("root", None),
        ("old-parent", "root"),
        ("new-parent", "root"),
        ("old-sibling", "old-parent"),
        ("moved", "old-parent"),
    ]
    for version, moved_parent in ((previous, "old-parent"), (current, "new-parent")):
        for index, (folder_id, parent_id) in enumerate(nodes):
            actual_parent = moved_parent if folder_id == "moved" else parent_id
            DocmindFolderVersion.create(
                id=f"{version.id}-{folder_id}",
                version_id=version.id,
                folder_id=folder_id,
                l0_text="한국어 L0",
                l1_text="한국어 L1",
                l0_hash="c" * 64,
                l1_hash="d" * 64,
                generator_metadata={},
                parent_folder_id=actual_parent,
                source_file_id=f"source-{folder_id}",
                relative_path=f"GMP/{folder_id}",
                display_name=folder_id,
                ordinal=index,
                depth=0 if folder_id == "root" else 1,
                **docmind_catalog_service._timestamps(),
            )

    affected = service._hierarchy_affected_nodes(current)

    assert {"moved", "old-parent", "new-parent", "old-sibling", "root"} <= affected
