from types import SimpleNamespace

import pytest
import requests

from rag.llm.rerank_model import KURERerank


def test_merges_reversed_batch_indexes_without_rescaling(monkeypatch):
    calls = []

    def post(url, *, json, **kwargs):
        assert url == KURERerank.URL and kwargs["allow_redirects"] is False
        n = len(json["documents"])
        assert json["top_n"] == n and json["return_documents"] is False
        calls.append(n)
        results = [{"index": i, "relevance_score": int(json["documents"][i])/256} for i in reversed(range(n))]
        return SimpleNamespace(status_code=200, json=lambda: {
            "model": KURERerank.MODEL, "results": results, "usage": {"total_tokens": n}})

    monkeypatch.setattr(requests, "post", post)
    scores, tokens = KURERerank("synthetic").similarity("query", list(map(str, range(128))), normalize=False)
    assert list(scores) == [i/256 for i in range(128)] and calls == [64, 64] and tokens == 128


@pytest.mark.parametrize("failure", ["wrong_model", "duplicate", "out_of_range", "nan", "missing"])
def test_rejects_invalid_response(monkeypatch, failure):
    records = [{"index": 0, "relevance_score": .7}, {"index": 1, "relevance_score": .3}]
    model = KURERerank.MODEL
    if failure == "wrong_model":
        model = "different-model"
    elif failure == "duplicate":
        records[1]["index"] = 0
    elif failure == "missing":
        records.pop()
    else:
        records[1]["relevance_score"] = 1.1 if failure == "out_of_range" else float("nan")
    resp = SimpleNamespace(status_code=200, json=lambda: {"model": model, "results": records, "usage": {"total_tokens": 3}})
    monkeypatch.setattr(requests, "post", lambda *args, **kw: resp)
    with pytest.raises(ValueError):
        KURERerank("synthetic").similarity("query", ["a", "b"])


def test_rate_limit_longer_than_search_deadline_fails_without_partial_results(monkeypatch):
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(status_code=429, headers={"Retry-After": "70"})
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(RuntimeError, match="429"):
        KURERerank("synthetic").similarity("q", ["x"] * 128)
    assert len(calls) == 1
