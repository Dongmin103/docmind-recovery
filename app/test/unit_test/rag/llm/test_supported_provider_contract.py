import pytest
from rag import llm
from rag.llm.chat_model import LiteLLMBase
from rag.llm.model_meta import OpenAIAPICompatible


def test_only_supported_provider_transport_registrations():
    assert set(llm.ChatModel) == {'OpenAI', 'OpenAI-API-Compatible'}
    assert set(llm.EmbeddingModel) == {'OpenAI', 'OpenAI-API-Compatible'}
    assert set(llm.CvModel) == {'OpenAI', 'OpenAI-API-Compatible'}
    assert set(llm.RerankModel) == {'Jina', 'KURE'}
    assert set(llm.ModelMeta) == {'OpenAI-API-Compatible'}


def test_discovery_does_not_advertise_unusable_models():
    models = OpenAIAPICompatible('synthetic', 'http://example.test/v1')._format_model_list({'data': [
        {'id': 'gte-multilingual-base', 'max_model_len': 8192}, {'id': 'gpt-4o'},
        {'id': 'whisper'}, {'id': 'tts-1'}, {'id': 'jina-reranker-v3.5'},
    ]})
    assert [m['name'] for m in models] == ['gte-multilingual-base', 'gpt-4o']
    assert models[0]['model_types'] == ['embedding']
    assert models[1]['model_types'] == ['chat', 'vision']


def test_official_chat_rejects_retired_provider_before_request():
    with pytest.raises(ValueError, match='Unsupported'):
        LiteLLMBase('synthetic', 'model', provider='Bedrock')

from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
from rag.llm.chat_model import OpenAI_APIChat
from rag.llm.model_meta import Base as ModelMetaBase


@pytest.mark.parametrize('credential,expected', [('plain', 'plain'), ('{"api_key":"synthetic"}', 'synthetic'), ('{"other":"value"}', ''), ('["value"]', '["value"]')])
def test_supported_discovery_credential_envelopes(credential, expected):
    assert ModelMetaBase(credential)._get_api_key() == expected


def _chat_adapter(monkeypatch, provider, response):
    import rag.llm.chat_model as transport
    if provider == 'OpenAI':
        adapter = LiteLLMBase('synthetic', 'gpt-4o', provider='OpenAI', max_retries=0)
        request = AsyncMock(return_value=response)
        monkeypatch.setattr(transport.litellm, 'acompletion', request)
    else:
        adapter = OpenAI_APIChat('synthetic', 'gpt-4o___OpenAI-API', base_url='http://example.test/v1', max_retries=0)
        request = AsyncMock(return_value=response)
        adapter.async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=request)))
    return adapter, request


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['OpenAI', 'OpenAI-API-Compatible'])
async def test_ordinary_chat_preserves_text_history_and_usage(monkeypatch, provider):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=' answer '), finish_reason='stop')], usage=SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10))
    adapter, request = _chat_adapter(monkeypatch, provider, response)
    history = [{'role': 'user', 'content': 'question'}]
    answer, tokens = await adapter.async_chat('system', history, {'temperature': 0.2, 'model_type': 'chat'})
    assert (answer, tokens) == ('answer', 10)
    assert adapter.last_usage == {'prompt_tokens': 6, 'completion_tokens': 4, 'total_tokens': 10}
    args = request.call_args.kwargs
    assert args['messages'] == [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'question'}]
    assert 'model_type' not in args
    if provider == 'OpenAI':
        assert args['model'] == 'openai/gpt-4o'
    else:
        assert args['model'] == 'gpt-4o'


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['OpenAI', 'OpenAI-API-Compatible'])
async def test_ordinary_chat_stream_preserves_delta_and_total(monkeypatch, provider):
    async def stream():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='answer'), finish_reason='stop')], usage=SimpleNamespace(prompt_tokens=6, completion_tokens=4, total_tokens=10))
    adapter, _ = _chat_adapter(monkeypatch, provider, stream())
    result = [chunk async for chunk in adapter.async_chat_streamly('system', [{'role': 'user', 'content': 'question'}], {})]
    assert result == ['answer', 10]


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['OpenAI', 'OpenAI-API-Compatible'])
async def test_ordinary_chat_error_and_cancellation(monkeypatch, provider):
    adapter, request = _chat_adapter(monkeypatch, provider, None)
    request.side_effect = ValueError('invalid request')
    answer, tokens = await adapter.async_chat('', [{'role': 'user', 'content': 'question'}], {})
    assert '**ERROR**' in answer and tokens == 0
    request.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await adapter.async_chat('', [{'role': 'user', 'content': 'question'}], {})
