"""Execute the real claim handler without importing the application bootstrap."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('size, status', [(2, 200), (1, 200), (True, 409), (0, 409), (3, 200), (4, 200), (8, 200), (12, 200), (13, 409), (-1, 409), (12.0, 409), ('2', 409)])
def test_signed_pool_claim_size(size, status):
    path = Path(__file__).resolve().parents[5] / 'api/apps/restful_apis/docmind_api.py'
    node = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'claim_cloud_sync_job')
    node.decorator_list = []
    class Error(Exception):
        def __init__(self, code): self.code = code
    async def body(): return b'signed'
    async def payload(**_): return {'worker_id': 'test', 'protocol_version': 1, 'ingestion_pool_size': size}
    async def authenticate(_): return 'key'
    calls = []
    scope = {'asyncio': asyncio, 'request': SimpleNamespace(get_data=body, get_json=payload),
             '_authenticate_worker': authenticate, '_claim_cloud_sync_job_in_thread': lambda *args: calls.append(args),
             '_signed_worker_response': lambda data, key, status=200: (status, data),
             'docmind_ingestion_service': SimpleNamespace(DocmindIngestionError=Error),
             'docmind_worker_auth': SimpleNamespace(WorkerAuthenticationError=type('AuthError', (Exception,), {}))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
    actual, data = asyncio.run(scope['claim_cloud_sync_job']())
    assert actual == status
    if status == 200: assert calls[0][-1] == size and data == {'job': None}
    else: assert calls == [] and data['error'] == 'DOCMIND_INGESTION_REQUEST_INVALID'
