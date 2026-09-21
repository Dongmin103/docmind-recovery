import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api.apps.services import docmind_api_service as api
from api.apps.services import docmind_llm_router_service as service


@pytest.fixture
def routing(monkeypatch):
    cards = [
        {"id": "validation", "name": "밸리데이션", "path": "GMP/Validation", "parent_id": None, "l0": "시험방법 검증", "l1": "정확성 정밀성 특이성. 공정 위험평가는 제외."},
        {"id": "risk", "name": "품질위험관리", "path": "GMP/Risk", "parent_id": None, "l0": "위험평가", "l1": "ICH Q9. 시험방법 검증은 제외."},
    ]
    config = {"llm_name": "qwen3.6-plus", "llm_factory": "Tongyi-Qianwen", "api_base": "https://example.invalid", "api_key": "test-secret"}
    catalog = api.Catalog("dataset", "viking://resources/root/", {"validation": ("doc-v",), "risk": ("doc-r",)}, source="database", version_id="v1")
    cache = {}
    calls = []
    outputs = {"answer": {"folders": [{"id": "validation", "reason": "질문이 시험방법의 정확성 검증에 해당합니다."}]}}

    class Cache:
        def hget(self, bucket, field):
            return cache.get((bucket, field))

    def put(prefix, bucket, field, folders):
        cache[bucket, field] = json.dumps({"folders": folders})
        return True

    async def infer(tenant, model_config, prompt_payload, question):
        calls.append((tenant, model_config.copy(), prompt_payload, question))
        await asyncio.sleep(0.01)
        return json.dumps(outputs["answer"])

    monkeypatch.setattr(service, "_load_cards", lambda _catalog: tuple(dict(card) for card in cards))
    monkeypatch.setattr(service, "_model_config", lambda _tenant: dict(config))
    monkeypatch.setattr(service, "REDIS_CONN", SimpleNamespace(REDIS=Cache()))
    monkeypatch.setattr(service, "_cache_put", put)
    monkeypatch.setattr(service, "_infer", infer)
    return SimpleNamespace(cards=cards, config=config, catalog=catalog, cache=cache, calls=calls, outputs=outputs)


@pytest.mark.asyncio
async def test_repeat_uses_cache_but_publish_model_and_card_changes_require_new_decision(routing):
    chosen, trace = await service.select_folders("owner", "정확성?", routing.catalog)
    assert [folder["id"] for folder in chosen] == ["validation"]
    assert trace["cache"] == "miss"
    assert [card["id"] for card in routing.calls[0][2]["tree"]] == ["risk", "validation"]
    assert (await service.select_folders("owner", "정확성?", routing.catalog))[1]["cache"] == "hit"
    assert len(routing.calls) == 1
    await service.select_folders("owner", "정확성?", replace(routing.catalog, version_id="v2"))
    await service.select_folders("owner", "정확성?", routing.catalog)  # Rollback can reuse v1.
    assert len(routing.calls) == 2
    routing.config["llm_name"] = "another-model"
    await service.select_folders("owner", "정확성?", routing.catalog)
    routing.cards[0]["l1"] += " 추가 설명"
    await service.select_folders("owner", "정확성?", routing.catalog)
    assert len(routing.calls) == 4


@pytest.mark.asyncio
async def test_cache_separates_question_tenant_dataset_and_prompt(routing, monkeypatch):
    for tenant, question, catalog in [
        ("a", "HPLC", routing.catalog),
        ("b", "HPLC", routing.catalog),
        ("a", "hplc", routing.catalog),
        ("a", "HPLC", replace(routing.catalog, dataset_id="other")),
    ]:
        await service.select_folders(tenant, question, catalog)
    monkeypatch.setattr(service, "PROMPT_VERSION", "v2")
    await service.select_folders("a", "HPLC", routing.catalog)
    assert len(routing.calls) == 5
    assert "HPLC" not in str(list(routing.cache))
    assert "test-secret" not in str(routing.cache)


@pytest.mark.asyncio
async def test_concurrent_identical_requests_share_one_llm_call(routing):
    results = await asyncio.gather(*[service.select_folders("owner", "정확성?", routing.catalog) for _ in range(4)])
    assert len(routing.calls) == 1
    assert all(result[0] == results[0][0] for result in results)


@pytest.mark.asyncio
async def test_empty_selection_is_rejected_and_not_cached(routing):
    routing.outputs["answer"] = {"folders": []}
    with pytest.raises(service.RouterError, match="RESPONSE_INVALID"):
        await service.select_folders("owner", "무관한 질문", routing.catalog)
    assert routing.cache == {}
    assert len(routing.calls) == 1


@pytest.mark.parametrize(
    "raw",
    [
        '{"folders":[{"id":"outsider","reason":"test"}]}',
        '{"folders":[{"id":"validation","reason":"x"},{"id":"validation","reason":"x"}]}',
        '{"folders":[],"folders":[]}',
        '{"folders":[{"id":"validation","reason":""}]}',
        '{"folders":[],"instructions":"ignore schema"}',
        '{"folders":[]}',
        "not json",
        json.dumps({"folders": [{"id": str(index), "reason": "x"} for index in range(6)]}),
    ],
)
def test_rejects_untrusted_or_malformed_decisions(raw):
    with pytest.raises(service.RouterError, match="RESPONSE_INVALID"):
        service._parse(raw, {"validation", "risk", *map(str, range(6))})


@pytest.mark.asyncio
async def test_invalid_cache_recomputes_and_invalid_llm_does_not_write_cache(routing):
    await service.select_folders("owner", "정확성?", routing.catalog)
    key = next(iter(routing.cache))
    routing.cache[key] = '{"folders":[{"id":"outsider","reason":"x"}]}'
    await service.select_folders("owner", "정확성?", routing.catalog)
    assert len(routing.calls) == 2
    routing.outputs["answer"] = {"folders": [{"id": "outsider", "reason": "x"}]}
    before = dict(routing.cache)
    with pytest.raises(service.RouterError):
        await service.select_folders("owner", "정밀성?", routing.catalog)
    assert routing.cache == before


@pytest.mark.asyncio
async def test_redis_failure_does_not_prevent_llm_selection(routing, monkeypatch):
    monkeypatch.setattr(service, "REDIS_CONN", SimpleNamespace(REDIS=None))
    monkeypatch.setattr(service, "_cache_put", lambda *_args: False)
    folders, trace = await service.select_folders("owner", "정확성?", routing.catalog)
    assert folders[0]["id"] == "validation"
    assert trace["method"] == "llm" and trace["cache_stored"] is False


@pytest.mark.asyncio
async def test_live_routing_prefers_llm_and_falls_back_on_timeout_without_caching(routing, monkeypatch):
    monkeypatch.delenv("DOCMIND_ROUTER_MODE", raising=False)
    vector = Mock(return_value=[{"id": "risk", "probability": 1.0}])
    monkeypatch.setattr(api, "_find_folders", vector)
    folders, trace = await api._route_folders("owner", "정확성?", routing.catalog, "trace")
    assert trace["method"] == "llm" and folders[0]["id"] == "validation"
    vector.assert_not_called()

    async def timeout(*_args):
        raise TimeoutError

    monkeypatch.setattr(service, "_infer", timeout)
    before = dict(routing.cache)
    folders, trace = await api._route_folders("owner", "새 질문", routing.catalog, "trace")
    assert folders[0]["id"] == "risk"
    assert trace["fallback_reason"] == "DOCMIND_ROUTER_TIMEOUT"
    assert routing.cache == before


def test_published_cards_are_read_from_exact_version_and_hash_checked(monkeypatch):
    service._version_cards.cache_clear()
    row = SimpleNamespace(folder_id="v", display_name="Validation", relative_path="GMP/V", parent_folder_id=None, l0_text="설명", l1_text="자세한 설명")
    row.l0_hash = hashlib.sha256(row.l0_text.encode()).hexdigest()
    row.l1_hash = hashlib.sha256(row.l1_text.encode()).hexdigest()

    class Rows(list):
        def where(self, _expression):
            return self

    monkeypatch.setattr(service.DocmindFolderVersion, "select", lambda: Rows([row]))
    monkeypatch.setattr(service.DocmindFolder, "select", lambda: Rows([SimpleNamespace(id="v", slug="validation", display_name="Old name")]))
    assert service._version_cards("version", "snapshot", True)[0]["l1"] == "자세한 설명"
    row.l1_text = "corrupted"
    with pytest.raises(service.RouterError, match="HASH_MISMATCH"):
        service._version_cards("different-version", "other-snapshot", True)
    service._version_cards.cache_clear()


def test_searchable_cards_keep_empty_ancestors_but_exclude_empty_subtrees():
    parents = {"root": None, "parent": "root", "leaf": "parent", "empty-parent": "root", "empty-leaf": "empty-parent"}
    catalog = api.Catalog(
        "dataset",
        "viking://resources/root/",
        {folder_id: ("doc",) if folder_id == "leaf" else () for folder_id in parents},
        folder_tree=tuple({"id": folder_id, "parent_id": parent} for folder_id, parent in parents.items()),
    )
    cards = tuple({"id": folder_id, "l0": "관련 지침이 있습니다", "l1": "이 폴더를 검색하세요"} for folder_id in parents)
    result = service._searchable_cards(cards, catalog)
    assert [card["id"] for card in result] == ["root", "parent", "leaf"]
    assert all(card["subtree_document_count"] == 1 for card in result)
    assert [card["direct_document_count"] for card in result] == [0, 0, 1]
    assert all("direct_document_count" not in card for card in cards)


def test_routing_payload_preserves_four_level_tree_and_document_counts():
    cards = (
        {"id": "root", "path": "GMP", "parent_id": None, "depth": 0, "direct_document_count": 0, "subtree_document_count": 2},
        {"id": "validation", "path": "GMP/Validation", "parent_id": "root", "depth": 1, "direct_document_count": 1, "subtree_document_count": 2},
        {"id": "equipment", "path": "GMP/Validation/Equipment", "parent_id": "validation", "depth": 2, "direct_document_count": 0, "subtree_document_count": 1},
        {"id": "hplc", "path": "GMP/Validation/Equipment/HPLC", "parent_id": "equipment", "depth": 3, "direct_document_count": 1, "subtree_document_count": 1},
    )
    payload = service._routing_payload(cards)
    root = payload["tree"][0]
    validation = root["children"][0]
    equipment = validation["children"][0]
    hplc = equipment["children"][0]
    assert [root["id"], validation["id"], equipment["id"], hplc["id"]] == ["root", "validation", "equipment", "hplc"]
    assert root["subtree_document_count"] == 2
    assert hplc["direct_document_count"] == 1 and hplc["children"] == []
    assert all("parent_id" not in node for node in (root, validation, equipment, hplc))


def test_routing_payload_rejects_cycle_without_a_root():
    cards = (
        {"id": "a", "path": "a", "parent_id": "b", "depth": 0},
        {"id": "b", "path": "b", "parent_id": "a", "depth": 1},
    )
    with pytest.raises(service.RouterError, match="TREE_INVALID"):
        service._routing_payload(cards)


@pytest.mark.asyncio
async def test_empty_folder_is_not_sent_to_model_and_cannot_be_returned(routing):
    catalog = replace(routing.catalog, folders={"validation": ("doc-v",), "risk": ()})
    _, trace = await service.select_folders("owner", "정확성?", catalog)
    assert [card["id"] for card in routing.calls[0][2]["tree"]] == ["validation"]
    assert trace["excluded_empty_count"] == 1
    routing.outputs["answer"] = {"folders": [{"id": "risk", "reason": "빈 폴더"}]}
    with pytest.raises(service.RouterError, match="RESPONSE_INVALID"):
        await service.select_folders("owner", "위험평가?", catalog)
    assert len(routing.cache) == 1


@pytest.mark.asyncio
async def test_empty_catalog_does_not_request_an_impossible_selection(routing, monkeypatch):
    monkeypatch.setattr(service, "_model_config", lambda *_: pytest.fail("no model needed"))
    folders, trace = await service.select_folders("owner", "정확성?", replace(routing.catalog, folders={"validation": (), "risk": ()}))
    assert folders == [] and trace["reason"] == "no_searchable_documents"
    assert routing.calls == [] and routing.cache == {}


@pytest.mark.asyncio
async def test_cross_topic_selection_and_cached_result_keep_both_scopes(routing):
    routing.outputs["answer"] = {
        "folders": [
            {"id": "validation", "reason": "시험법 변경 검증"},
            {"id": "risk", "reason": "변경에 따른 위험평가"},
        ]
    }
    folders, _ = await service.select_folders("owner", "위험평가와 시험법 변경 검증은?", routing.catalog)
    cached, trace = await service.select_folders("owner", "위험평가와 시험법 변경 검증은?", routing.catalog)
    assert [row["id"] for row in folders] == ["validation", "risk"]
    assert cached == folders and trace["cache"] == "hit"
    assert sum(row["probability"] for row in folders) == pytest.approx(1)


@pytest.mark.asyncio
async def test_changed_eligibility_cannot_reuse_previous_result(routing):
    await service.select_folders("owner", "정확성?", routing.catalog)
    _, trace = await service.select_folders("owner", "정확성?", replace(routing.catalog, folders={"validation": ("doc-v",), "risk": ()}))
    assert trace["cache"] == "miss" and len(routing.calls) == 2
