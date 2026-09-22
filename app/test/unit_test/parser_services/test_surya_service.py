from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.metadata
import importlib.util
import io
import json
import sys
import threading
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
    settings = None

    def __init__(self, _manager):
        self.observed_full_page_max_tokens = None
        self.observed_inference_timeout_seconds = None

    def __call__(self, _images, *, full_page):
        assert full_page is True
        self.observed_full_page_max_tokens = self.settings.SURYA_MAX_TOKENS_FULL_PAGE
        self.observed_inference_timeout_seconds = self.settings.SURYA_INFERENCE_TIMEOUT_SECONDS
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

    settings = types.SimpleNamespace(
        SURYA_INFERENCE_BACKEND="llamacpp",
        IMAGE_DPI_HIGHRES=144,
        SURYA_MAX_TOKENS_FULL_PAGE=12288,
        SURYA_INFERENCE_TIMEOUT_SECONDS=570,
    )
    _Predictor.settings = settings
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
    assert service.ENGINE.predictor.observed_full_page_max_tokens == 1024
    assert service.ENGINE.predictor.observed_inference_timeout_seconds == 600
    assert service.ENGINE.request_timeout_seconds == 1800
    assert service.settings.SURYA_MAX_TOKENS_FULL_PAGE == 12288
    assert service.settings.SURYA_INFERENCE_TIMEOUT_SECONDS == 570
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        service.SuryaEngine._verify_model_file(service.ENGINE.model_path, "0" * 64)


def test_health_stays_responsive_and_second_parse_is_rejected(monkeypatch, tmp_path) -> None:
    service = _load_service(monkeypatch, tmp_path)
    parse_started = threading.Event()
    release_parse = threading.Event()

    def blocking_parse(payload):
        parse_started.set()
        assert release_parse.wait(timeout=5)
        return {"parse_run_id": payload["parse_run_id"]}

    monkeypatch.setattr(service.ENGINE, "parse", blocking_parse)
    server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_address[1]
    first_response: dict[str, object] = {}

    def send_first_parse() -> None:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "POST",
            "/v1/parse-media",
            json.dumps({"task_kind": "office_media_parse", "parse_run_id": "run-1"}),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        first_response["status"] = response.status
        response.read()
        connection.close()

    request_thread = threading.Thread(target=send_first_parse)
    request_thread.start()
    try:
        assert parse_started.wait(timeout=5)

        health_connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        health_connection.request("GET", "/health")
        health_response = health_connection.getresponse()
        health_payload = json.loads(health_response.read())
        health_connection.close()
        assert health_response.status == 200
        assert health_payload["status"] == "ready"

        busy_connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        busy_connection.request(
            "POST",
            "/v1/parse-media",
            json.dumps({"task_kind": "office_media_parse", "parse_run_id": "run-2"}),
            {"Content-Type": "application/json"},
        )
        busy_response = busy_connection.getresponse()
        busy_payload = json.loads(busy_response.read())
        busy_connection.close()
        assert busy_response.status == 503
        assert busy_payload["code"] == "PARSER_SURYA_BUSY"
    finally:
        release_parse.set()
        request_thread.join(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    assert first_response["status"] == 200


def test_office_media_token_cap_is_restored_after_predictor_failure(monkeypatch, tmp_path) -> None:
    service = _load_service(monkeypatch, tmp_path)
    image = Image.new("RGB", (32, 20), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    media_bytes = output.getvalue()

    class FailingPredictor:
        def __call__(self, _images, *, full_page):
            assert full_page is True
            assert service.settings.SURYA_MAX_TOKENS_FULL_PAGE == 1024
            assert service.settings.SURYA_INFERENCE_TIMEOUT_SECONDS == 600
            raise RuntimeError("synthetic predictor failure")

    service.ENGINE.predictor = FailingPredictor()
    with pytest.raises(RuntimeError, match="synthetic predictor failure"):
        service.ENGINE.parse_office_media(
            {
                "parse_run_id": "run-failure",
                "media_id": "media-failure",
                "media_hash": hashlib.sha256(media_bytes).hexdigest(),
                "source_locator": "#/pictures/failure",
                "media_base64": base64.b64encode(media_bytes).decode("ascii"),
            }
        )

    assert service.settings.SURYA_MAX_TOKENS_FULL_PAGE == 12288
    assert service.settings.SURYA_INFERENCE_TIMEOUT_SECONDS == 570
