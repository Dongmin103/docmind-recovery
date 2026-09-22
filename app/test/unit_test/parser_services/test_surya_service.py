from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
SERVICE_PATH = ROOT / "parser_services" / "surya" / "service.py"


class _Prediction:
    def model_dump(self):
        return {
            "blocks": [
                {
                    "reading_order": 0,
                    "label": "Text",
                    "raw_label": "Text",
                    "html": "<p>surya media text</p>",
                    "bbox": [0, 0, 20, 10],
                    "polygon": None,
                    "skipped": False,
                    "error": False,
                }
            ]
        }


class _Predictor:
    def __init__(self, _manager):
        pass

    def __call__(self, _images, *, full_page):
        assert full_page is True
        return [_Prediction()]


def _load_service(monkeypatch, tmp_path):
    model = tmp_path / "surya-2.gguf"
    mmproj = tmp_path / "surya-2-mmproj.gguf"
    model.write_bytes(b"pinned-model")
    mmproj.write_bytes(b"pinned-mmproj")
    monkeypatch.setenv("SURYA_GGUF_LOCAL_MODEL_PATH", str(model))
    monkeypatch.setenv("SURYA_GGUF_LOCAL_MMPROJ_PATH", str(mmproj))
    monkeypatch.setenv("SURYA_GGUF_MODEL_SHA256", hashlib.sha256(model.read_bytes()).hexdigest())
    monkeypatch.setenv("SURYA_GGUF_MMPROJ_SHA256", hashlib.sha256(mmproj.read_bytes()).hexdigest())

    settings = types.SimpleNamespace(SURYA_INFERENCE_BACKEND="llamacpp", IMAGE_DPI_HIGHRES=144)
    monkeypatch.setitem(sys.modules, "surya", types.ModuleType("surya"))
    monkeypatch.setitem(
        sys.modules,
        "surya.inference",
        types.SimpleNamespace(SuryaInferenceManager=lambda **_kwargs: object()),
    )
    monkeypatch.setitem(sys.modules, "surya.input", types.ModuleType("surya.input"))
    monkeypatch.setitem(sys.modules, "surya.input.load", types.SimpleNamespace(load_from_file=lambda *_a, **_k: ([], None)))
    monkeypatch.setitem(sys.modules, "surya.recognition", types.SimpleNamespace(RecognitionPredictor=_Predictor))
    monkeypatch.setitem(sys.modules, "surya.settings", types.SimpleNamespace(settings=settings))
    monkeypatch.setattr(importlib.metadata, "version", lambda _package: "0.22.1")

    spec = importlib.util.spec_from_file_location("docmind_test_surya_service", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_office_media_endpoint_engine_verifies_models_and_returns_manifest(monkeypatch, tmp_path) -> None:
    service = _load_service(monkeypatch, tmp_path)
    image = Image.new("RGB", (32, 20), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    media_bytes = output.getvalue()

    response = service.ENGINE.parse_office_media(
        {
            "parse_run_id": "run-1",
            "media_id": "media-1",
            "media_hash": hashlib.sha256(media_bytes).hexdigest(),
            "source_locator": "#/pictures/0",
            "media_base64": base64.b64encode(media_bytes).decode("ascii"),
        }
    )

    assert service.ENGINE.manifest()["model_files_verified"] is True
    assert response["task_kind"] == "office_media_parse"
    assert response["blocks"][0]["html"] == "<p>surya media text</p>"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        service.SuryaEngine._verify_model_file(service.ENGINE.model_path, "0" * 64)
