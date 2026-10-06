"""Load the answer service without application startup or model/storage SDKs."""

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture
def service(monkeypatch):
    path = Path(__file__).resolve().parents[3] / "api/apps/services/docmind_answer_service.py"
    spec = importlib.util.spec_from_file_location("answer_under_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module
