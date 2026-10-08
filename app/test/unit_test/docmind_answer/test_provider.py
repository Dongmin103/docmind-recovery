"""Real config/client code with synthetic secrets and HTTP-boundary fixtures only."""

import asyncio
import json
import sys

import httpx
import pytest


@pytest.fixture
def external_env(monkeypatch, tmp_path):
    secret = tmp_path / "key.txt"
    secret.write_text("sk-or-v1-synthetic-test-only\n", encoding="utf-8")
    monkeypatch.setenv("DOCMIND_ANSWER_ENABLED", "1")
    monkeypatch.setenv("DOCMIND_ANSWER_PROVIDER", "openrouter")
    monkeypatch.setenv("DOCMIND_ANSWER_MODEL", "qwen/qwen3.8-27b")
    monkeypatch.setenv("DOCMIND_ANSWER_EXTERNAL_ALLOWED", "1")
    monkeypatch.setenv("DOCMIND_OPENROUTER_API_KEY_FILE", str(secret))
    return secret


def generated(status="answered", answer="근거 답변 [1]", citations=None):
    return json.dumps({"status": status, "answer": answer, "citations": [1] if citations is None else citations}, ensure_ascii=False)


def completion(content='{"status":"answered","answer":"근거 답변 [1]","citations":[1]}', finish="stop", **extra):
    return {
        "id": "gen-synthetic",
        "model": "qwen/qwen3.8-27b",
        "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}, **extra}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cost": 0.001},
    }


def call(service, config, respond, citation_count=1):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await service.generate(config, [{"role": "user", "content": "synthetic evidence"}], citation_count, client=client)

    return asyncio.run(run())


def test_external_provider_requires_explicit_egress_opt_in(service, external_env, monkeypatch):
    monkeypatch.delenv("DOCMIND_ANSWER_EXTERNAL_ALLOWED")
    with pytest.raises(service.AnswerError, match="CONFIG_INVALID"):
        service.AnswerConfig.from_env()


def test_openrouter_wire_contract_and_secret_not_in_repr(service, external_env, monkeypatch):
    monkeypatch.setenv("DOCMIND_OPENROUTER_PROVIDER", "synthetic-provider")
    config = service.AnswerConfig.from_env()
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=completion())

    result = call(service, config, respond)
    assert (result.status, result.answer, result.references) == ("answered", "근거 답변 [1]", (1,))
    assert len(seen) == 1
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/chat/completions"
    assert seen[0].headers["Authorization"] == "Bearer sk-or-v1-synthetic-test-only"
    assert json.loads(seen[0].content) == {
        "model": "qwen/qwen3.8-27b",
        "messages": [{"role": "user", "content": "synthetic evidence"}],
        "stream": False,
        "max_tokens": 1024,
        "temperature": 0,
        "reasoning": {"enabled": False},
        "transforms": [],
        "provider": {"allow_fallbacks": False, "require_parameters": True, "data_collection": "deny", "zdr": True, "only": ["synthetic-provider"]},
    }
    assert "sk-or" not in repr(config)
    assert "sk-or" not in seen[0].content.decode()


@pytest.mark.parametrize("provider", ["ollama", "openrouter", "openai"])
def test_prompt_requires_inline_citations_and_sends_a_valid_synthetic_json_example(service, provider):
    config = service.AnswerConfig("http://local:11434", "synthetic", 8192, 1024, provider)
    chunks = [
        {"doc_id": "synthetic-doc", "chunk_id": "a", "content_with_weight": "합성 작업의 담당자는 민수입니다."},
        {"doc_id": "synthetic-doc", "chunk_id": "b", "content_with_weight": "합성 작업의 점검 주기는 7일입니다."},
    ]
    _, messages = service.pack_context("담당자와 점검 주기는?", chunks, chunks, 4000)
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        content = generated(answer="담당자는 민수입니다 [1]. 점검 주기는 7일입니다 [2].", citations=[1, 2])
        body = {"done": True, "message": {"content": content}} if provider == "ollama" else completion(content)
        return httpx.Response(200, json=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await service.generate(config, messages, 2, client=client)

    result = asyncio.run(run())
    assert (result.status, result.references) == ("answered", (1, 2))
    assert len(seen) == 1
    prompt = seen[0]["messages"][0]["content"]
    assert "citations 배열만 채우는 것으로는 충분하지 않습니다." in prompt
    assert "answer 문자열 안의 [N] 번호 집합과 citations 배열의 번호 집합은 정확히 같아야 합니다." in prompt
    _, marker, example_json = prompt.partition("합성 출력 형식 예시(실제 질문의 근거가 아님): ")
    assert marker
    example = json.loads(example_json)
    assert set(example) == {"status", "answer", "citations"}
    assert example["status"] == "answered"
    assert example["citations"] == [1, 2]
    assert "[1]" in example["answer"] and "[2]" in example["answer"]
    evidence = json.loads(seen[0]["messages"][1]["content"])["evidence"]
    assert [row["evidence_id"] for row in evidence] == [1, 2]


@pytest.mark.parametrize("secret", ["", "bad key\nheader", "sk-or-" + "x" * 9000])
def test_invalid_secret_fails_closed_without_exposing_it(service, external_env, secret):
    external_env.write_text(secret, encoding="utf-8")
    with pytest.raises(service.AnswerError) as error:
        service.AnswerConfig.from_env()
    assert str(error.value) == "DOCMIND_ANSWER_CONFIG_INVALID"


def test_missing_key_file_is_sanitized(service, external_env):
    external_env.unlink()
    with pytest.raises(service.AnswerError, match="CONFIG_INVALID") as error:
        service.AnswerConfig.from_env()
    assert str(external_env) not in str(error.value)


@pytest.mark.parametrize(
    "variable,value",
    [
        ("DOCMIND_ANSWER_PROVIDER", "unknown"),
        ("DOCMIND_ANSWER_CONTEXT_TOKENS", "bad"),
        ("DOCMIND_ANSWER_OUTPUT_TOKENS", "9000"),
        ("DOCMIND_ANSWER_MODEL", ""),
    ],
)
def test_invalid_configuration_fails_closed(service, external_env, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(service.AnswerError, match="CONFIG_INVALID"):
        service.AnswerConfig.from_env()


def test_local_switch_never_reads_or_sends_external_credentials(service, external_env, monkeypatch):
    external_env.unlink()
    monkeypatch.delenv("DOCMIND_ANSWER_EXTERNAL_ALLOWED")
    monkeypatch.setenv("DOCMIND_ANSWER_PROVIDER", "ollama")
    monkeypatch.setenv("DOCMIND_ANSWER_MODEL", "installed-local-model")
    monkeypatch.setenv("DOCMIND_ANSWER_CONTEXT_TOKENS", "8192")
    config = service.AnswerConfig.from_env()

    def respond(request):
        assert str(request.url) == "http://172.16.5.65:11434/api/chat"
        assert "Authorization" not in request.headers
        assert json.loads(request.content) == {
            "model": "installed-local-model",
            "messages": [{"role": "user", "content": "synthetic evidence"}],
            "stream": False,
            "options": {"num_ctx": 8192, "num_predict": 1024, "temperature": 0},
        }
        return httpx.Response(200, json={"done": True, "message": {"content": generated(answer="local [1]")}})

    result = call(service, config, respond)
    assert (result.status, result.answer, result.references) == ("answered", "local [1]", (1,))


@pytest.mark.parametrize(
    "body,code",
    [
        (completion(finish="length"), "OUTPUT_TRUNCATED"),
        (completion(content=generated(answer="uncited")), "CITATIONS_INVALID"),
        (completion(content=generated(answer="unknown [2]", citations=[2])), "CITATIONS_INVALID"),
        (completion(content=None), "RESPONSE_INVALID"),
        (completion(finish="content_filter"), "RESPONSE_INVALID"),
        (completion(finish="tool_calls"), "RESPONSE_INVALID"),
        (completion(error={"message": "private upstream error"}), "RESPONSE_INVALID"),
        ({"choices": []}, "RESPONSE_INVALID"),
        ({"choices": None}, "RESPONSE_INVALID"),
        ({"error": {"message": "private provider error"}}, "RESPONSE_INVALID"),
        ([], "RESPONSE_INVALID"),
    ],
)
@pytest.mark.parametrize("provider", ["openrouter", "openai"])
def test_openai_protocol_invalid_answers_are_not_returned(service, external_env, monkeypatch, provider, body, code):
    monkeypatch.setenv("DOCMIND_ANSWER_PROVIDER", provider)
    config = service.AnswerConfig.from_env()
    with pytest.raises(service.AnswerError, match=code) as error:
        call(service, config, lambda _: httpx.Response(200, json=body))
    assert "private" not in str(error.value)


@pytest.mark.parametrize("status,code", [(301, "UNAVAILABLE"), (401, "AUTH_FAILED"), (402, "CREDITS_REQUIRED"), (429, "RATE_LIMITED"), (503, "UNAVAILABLE")])
def test_http_failure_never_retries_or_falls_back(service, external_env, status, code):
    config = service.AnswerConfig.from_env()
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(status, json={"error": "private"}, headers={"Location": "https://other.invalid"})

    with pytest.raises(service.AnswerError, match=code):
        call(service, config, respond)
    assert seen == ["https://openrouter.ai/api/v1/chat/completions"]


def test_external_response_size_is_bounded(service, external_env):
    with pytest.raises(service.AnswerError, match="RESPONSE_INVALID"):
        call(service, service.AnswerConfig.from_env(), lambda _: httpx.Response(200, content=b"x" * 1_048_577))


@pytest.mark.parametrize("provider", ["ollama", "openrouter", "openai"])
@pytest.mark.parametrize(
    "status,answer,citations,expected_answer,expected_refs",
    [
        ("answered", "  합성 사실 [1], 확인 [1]  ", [1], "합성 사실 [1], 확인 [1]", (1,)),
        ("partial", "합성 담당자는 민수입니다 [1]. 시행일은 확인할 수 없습니다.", [1], "합성 담당자는 민수입니다 [1]. 시행일은 확인할 수 없습니다.", (1,)),
        ("insufficient", "질문의 전제에 근거가 없습니다. 꾸며낸 사실 [999]", [], "선택한 문서에서 질문의 전제나 답을 뒷받침할 근거를 찾지 못했습니다.", ()),
        ("insufficient", "", [], "선택한 문서에서 질문의 전제나 답을 뒷받침할 근거를 찾지 못했습니다.", ()),
    ],
)
def test_generated_states_use_one_call_and_server_owned_insufficient_answer(service, provider, status, answer, citations, expected_answer, expected_refs):
    config = service.AnswerConfig("http://local:11434", "synthetic", 8192, 1024, provider)
    seen = []

    def respond(request):
        seen.append(request)
        content = generated(status, answer, citations)
        body = {"done": True, "message": {"content": content}} if provider == "ollama" else completion(content)
        return httpx.Response(200, json=body)

    result = call(service, config, respond)
    assert (result.status, result.answer, result.references) == (status, expected_answer, expected_refs)
    assert len(seen) == 1


@pytest.mark.parametrize("provider", ["ollama", "openrouter", "openai"])
@pytest.mark.parametrize(
    "content,code",
    [
        ("선택한 문서 근거만으로는 답변할 수 없습니다.", "RESPONSE_INVALID"),
        ("private malformed {", "RESPONSE_INVALID"),
        ("```json\n" + generated() + "\n```", "RESPONSE_INVALID"),
        (generated() + " trailing", "RESPONSE_INVALID"),
        ("[]", "RESPONSE_INVALID"),
        ("null", "RESPONSE_INVALID"),
        ('{"status":"answered","answer":"private [1]","citations":[NaN]}', "RESPONSE_INVALID"),
        ('{"status":"unknown","answer":"private","citations":[]}', "RESPONSE_INVALID"),
        ('{"status":[],"answer":"private","citations":[]}', "RESPONSE_INVALID"),
        ('{"status":"answered","citations":[1]}', "RESPONSE_INVALID"),
        ('{"status":"answered","answer":"private [1]","citations":[1],"extra":true}', "RESPONSE_INVALID"),
        ('{"status":"answered","answer":null,"citations":[1]}', "RESPONSE_INVALID"),
        ('{"status":"answered","answer":"private [1]","citations":1}', "RESPONSE_INVALID"),
        ('{"status":"answered","status":"insufficient","answer":"private","citations":[]}', "RESPONSE_INVALID"),
        ('{"status":"insufficient","answer":"","answer":"private","citations":[]}', "RESPONSE_INVALID"),
        (generated(citations=[True]), "RESPONSE_INVALID"),
        (generated(citations=[1.0]), "RESPONSE_INVALID"),
        (generated(citations=["1"]), "RESPONSE_INVALID"),
        (generated(citations=[None]), "RESPONSE_INVALID"),
        (generated("insufficient", "private", [True]), "RESPONSE_INVALID"),
        (generated(answer="   "), "RESPONSE_INVALID"),
        (generated(answer="private"), "CITATIONS_INVALID"),
        (generated(citations=[]), "CITATIONS_INVALID"),
        (generated(citations=[1, 1]), "CITATIONS_INVALID"),
        (generated(answer="private [0]", citations=[0]), "CITATIONS_INVALID"),
        (generated(answer="private [2]", citations=[1]), "CITATIONS_INVALID"),
        (generated(answer="private [1] [2]", citations=[1]), "CITATIONS_INVALID"),
        (generated(answer="private [1]", citations=[2]), "CITATIONS_INVALID"),
        (generated("partial", "private", [1]), "CITATIONS_INVALID"),
        (generated("insufficient", "private", [1]), "CITATIONS_INVALID"),
    ],
)
def test_content_schema_and_citation_integrity_fail_closed(service, provider, content, code):
    config = service.AnswerConfig("http://local:11434", "synthetic", 8192, 1024, provider)
    body = {"done": True, "message": {"content": content}} if provider == "ollama" else completion(content)
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=body)

    with pytest.raises(service.AnswerError) as error:
        call(service, config, respond)
    assert str(error.value) == f"DOCMIND_LLM_{code}"
    assert len(seen) == 1


@pytest.mark.parametrize("provider", ["ollama", "openrouter", "openai"])
@pytest.mark.parametrize("citations", [[1], [2]])
def test_declared_and_inline_citations_must_agree_even_when_all_ids_are_in_range(service, provider, citations):
    config = service.AnswerConfig("http://local:11434", "synthetic", 8192, 1024, provider)
    content = generated(answer="합성 사실 [1] [2]", citations=citations)
    body = {"done": True, "message": {"content": content}} if provider == "ollama" else completion(content)
    with pytest.raises(service.AnswerError, match="CITATIONS_INVALID"):
        call(service, config, lambda _: httpx.Response(200, json=body), citation_count=2)


def test_multiple_declared_references_are_validated_as_a_set(service):
    config = service.AnswerConfig("http://local:11434", "synthetic", 8192, 1024, "ollama")
    content = generated(answer="합성 사실 [1] [2] [1]", citations=[2, 1])
    result = call(service, config, lambda _: httpx.Response(200, json={"done": True, "message": {"content": content}}), citation_count=2)
    assert (result.status, result.answer, result.references) == ("answered", "합성 사실 [1] [2] [1]", (2, 1))


@pytest.mark.parametrize("provider", ["ollama", "openrouter", "openai"])
def test_deeply_nested_json_is_a_sanitized_format_error(service, provider):
    config = service.AnswerConfig("http://local:11434", "synthetic", 8192, 1024, provider)
    recursion_limit = sys.getrecursionlimit()
    depth = max(20_000, recursion_limit * 2)
    content = "[" * depth + "0" + "]" * depth
    body = {"done": True, "message": {"content": content}} if provider == "ollama" else completion(content)
    assert len(json.dumps(body).encode()) < 1_048_576
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=body)

    with pytest.raises(service.AnswerError) as error:
        call(service, config, respond)
    assert str(error.value) == "DOCMIND_LLM_RESPONSE_INVALID"
    assert len(seen) == 1
    assert sys.getrecursionlimit() == recursion_limit


def test_default_local_openai_call_does_not_read_or_send_external_credentials(service, monkeypatch, tmp_path):
    monkeypatch.setenv("DOCMIND_ANSWER_ENABLED", "1")
    monkeypatch.setenv("DOCMIND_OPENROUTER_API_KEY_FILE", str(tmp_path / "missing-key"))
    config = service.AnswerConfig.from_env()

    def respond(request):
        assert str(request.url) == "http://128.134.187.112:13434/v1/chat/completions"
        assert "Authorization" not in request.headers
        assert json.loads(request.content) == {
            "model": "qwen3.8-flash-next",
            "messages": [{"role": "user", "content": "synthetic evidence"}],
            "stream": False,
            "max_tokens": 1024,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        return httpx.Response(200, json=completion())

    result = call(service, config, respond)
    assert (result.status, result.answer, result.references) == ("answered", "근거 답변 [1]", (1,))


@pytest.mark.parametrize("base_url", ["http://local:13434/v1", "http://local:13434/v1/"])
def test_local_openai_configured_base_url_is_used(service, monkeypatch, base_url):
    monkeypatch.setenv("DOCMIND_ANSWER_ENABLED", "1")
    monkeypatch.setenv("DOCMIND_ANSWER_BASE_URL", base_url)
    monkeypatch.setenv("DOCMIND_ANSWER_MODEL", "installed-model")
    config = service.AnswerConfig.from_env()

    def respond(request):
        assert str(request.url) == "http://local:13434/v1/chat/completions"
        assert json.loads(request.content)["model"] == "installed-model"
        return httpx.Response(200, json=completion())

    assert call(service, config, respond).status == "answered"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://user:password@local:13434/v1",
        "http://local:13434/v1/models",
        "http://local:13434/v1?key=value",
        "http://local:13434/v1#fragment",
        "ftp://local:13434/v1",
        "http://local:13434",
        "http://local:bad/v1",
    ],
)
def test_local_openai_invalid_url_fails_before_request(service, monkeypatch, base_url):
    monkeypatch.setenv("DOCMIND_ANSWER_ENABLED", "1")
    monkeypatch.setenv("DOCMIND_ANSWER_BASE_URL", base_url)
    with pytest.raises(service.AnswerError, match="CONFIG_INVALID"):
        service.AnswerConfig.from_env()


@pytest.mark.parametrize("status,code", [(301, "UNAVAILABLE"), (401, "AUTH_FAILED"), (429, "RATE_LIMITED"), (503, "UNAVAILABLE")])
def test_local_openai_http_failure_has_no_external_fallback(service, monkeypatch, status, code):
    monkeypatch.setenv("DOCMIND_ANSWER_ENABLED", "1")
    config = service.AnswerConfig.from_env()
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(status, json={"error": "private"}, headers={"Location": "https://other.invalid"})

    with pytest.raises(service.AnswerError, match=code):
        call(service, config, respond)
    assert seen == ["http://128.134.187.112:13434/v1/chat/completions"]
