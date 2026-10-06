"""Real config/client code with synthetic secrets and HTTP-boundary fixtures only."""

import asyncio
import json

import httpx
import pytest


@pytest.fixture
def external_env(monkeypatch, tmp_path):
    secret = tmp_path / "key.txt"
    secret.write_text("sk-or-v1-synthetic-test-only\n", encoding="utf-8")
    monkeypatch.setenv("DOCMIND_ANSWER_ENABLED", "1")
    monkeypatch.setenv("DOCMIND_ANSWER_EXTERNAL_ALLOWED", "1")
    monkeypatch.setenv("DOCMIND_OPENROUTER_API_KEY_FILE", str(secret))
    return secret


def completion(content="근거 답변 [1]", finish="stop", **extra):
    return {
        "id": "gen-synthetic",
        "model": "qwen/qwen3.8-27b",
        "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}, **extra}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "cost": 0.001},
    }


def call(service, config, respond):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await service.generate(config, [{"role": "user", "content": "synthetic evidence"}], 1, client=client)

    return asyncio.run(run())


def test_external_default_requires_explicit_egress_opt_in(service, external_env, monkeypatch):
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

    assert call(service, config, respond) == "근거 답변 [1]"
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
        return httpx.Response(200, json={"done": True, "message": {"content": "local [1]"}})

    assert call(service, config, respond) == "local [1]"


@pytest.mark.parametrize(
    "body,code",
    [
        (completion(finish="length"), "OUTPUT_TRUNCATED"),
        (completion(content="uncited"), "CITATIONS_INVALID"),
        (completion(content="unknown [2]"), "CITATIONS_INVALID"),
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
def test_openrouter_invalid_answers_are_not_returned(service, external_env, body, code):
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


def test_external_exact_abstention_is_allowed(service, external_env):
    result = call(service, service.AnswerConfig.from_env(), lambda _: httpx.Response(200, json=completion(content="선택한 문서 근거만으로는 답변할 수 없습니다.")))
    assert result == "선택한 문서 근거만으로는 답변할 수 없습니다."
