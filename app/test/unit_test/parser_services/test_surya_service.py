from __future__ import annotations

import base64
import hashlib
import http.client
import importlib.metadata
import importlib.util
import io
import json
import os
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
        LLAMA_CPP_NGL=0,
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
    assert service.ENGINE.manifest()["gpu_layers_requested"] == 0
    assert service.ENGINE.manifest()["gpu_offload_verified"] is False
    assert response["task_kind"] == "office_media_parse"
    assert response["blocks"][0]["html"] == "<p>surya media text</p>"
    assert service.ENGINE.predictor.observed_full_page_max_tokens == 1024
    assert service.ENGINE.predictor.observed_inference_timeout_seconds == 600
    assert service.ENGINE.request_timeout_seconds == 1800
    assert service.settings.SURYA_MAX_TOKENS_FULL_PAGE == 12288
    assert service.settings.SURYA_INFERENCE_TIMEOUT_SECONDS == 570
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        service.SuryaEngine._verify_model_file(service.ENGINE.model_path, "0" * 64)


def test_gpu_health_requires_fresh_positive_cuda_offload_log_after_inference(monkeypatch, tmp_path) -> None:
    service = _load_service(monkeypatch, tmp_path)
    engine = service.ENGINE
    engine.gpu_layers_requested = 99
    engine.gpu_device_visible = True
    engine.manager = types.SimpleNamespace(backend=types.SimpleNamespace(handle=object()))
    engine._llama_log_path = tmp_path / "llamacpp_server.log"
    engine._llama_log_start = 0

    image = Image.new("RGB", (32, 20), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    media_bytes = output.getvalue()

    class GpuPredictor(_Predictor):
        def __call__(self, images, *, full_page):
            engine._llama_log_path.write_text(
                "load_tensors: offloaded 25/25 layers to GPU\n"
                "load_tensors:        CUDA0 model buffer size = 1024.00 MiB\n",
                encoding="utf-8",
            )
            return super().__call__(images, full_page=full_page)

    engine.predictor = GpuPredictor(engine.manager)
    assert engine.manifest()["gpu_offload_verified"] is False
    engine.parse_office_media(
        {
            "parse_run_id": "gpu-run",
            "media_id": "gpu-media",
            "media_hash": hashlib.sha256(media_bytes).hexdigest(),
            "source_locator": "#/pictures/gpu",
            "media_base64": base64.b64encode(media_bytes).decode("ascii"),
        }
    )
    assert engine.manifest()["gpu_layers_requested"] == 99
    assert engine.manifest()["gpu_device_visible"] is True
    assert engine.manifest()["gpu_offload_verified"] is True
    assert engine.manifest()["gpu_offloaded_layers"] == 25
    assert engine.manifest()["gpu_total_layers"] == 25
    engine.manager.backend.handle = object()
    assert engine.manifest()["gpu_offload_verified"] is False
    assert engine.manifest()["gpu_offloaded_layers"] is None


def test_gpu_execution_requires_matching_spawned_cuda_compute_process(monkeypatch, tmp_path) -> None:
    service = _load_service(monkeypatch, tmp_path)
    engine = service.ENGINE
    engine.gpu_layers_requested = 99
    pid, port = 160, 8765
    handle = types.SimpleNamespace(base_url=f"http://127.0.0.1:{port}/v1", spawned_by_us=True)
    engine.manager = types.SimpleNamespace(backend=types.SimpleNamespace(handle=handle))
    engine._llama_sentinel_path = tmp_path / "llamacpp_server.json"
    engine._llama_sentinel_path.write_text(json.dumps({"pid": pid, "port": port, "backend": "llamacpp"}))
    engine._proc_root = tmp_path / "proc"
    proc = engine._proc_root / str(pid)
    proc.mkdir(parents=True)
    executable = tmp_path / "llama-server"
    executable.write_bytes(b"binary")
    os.link(executable, proc / "exe")
    service.settings.LLAMA_CPP_BINARY = str(executable)
    (proc / "cmdline").write_bytes(
        b"/opt/llama/llama-server\0-m\0" + os.fsencode(engine.model_path)
        + b"\0--mmproj\0" + os.fsencode(engine.mmproj_path)
        + b"\0-ngl\099\0--port\08765\0"
    )
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    nvml = types.SimpleNamespace(returncode=0, stdout="161\n")
    monkeypatch.setattr(service.subprocess, "run", lambda *_a, **_k: nvml)

    assert engine.manifest()["gpu_execution_verified"] is False
    engine._verify_gpu_execution_from_process()
    assert engine.manifest()["gpu_execution_verified"] is False

    nvml.stdout = "160\n"
    engine._verify_gpu_execution_from_process()
    assert engine.manifest()["gpu_execution_verified"] is True
    assert engine.manifest()["gpu_execution_proof_source"] == "nvidia_smi_compute_pid"
    assert engine.manifest()["gpu_offload_verified"] is False
    assert engine.manifest()["gpu_offloaded_layers"] is None

    nvml.stdout = ""
    engine._verify_gpu_execution_from_process()  # Same handle keeps cached proof.
    assert engine.manifest()["gpu_execution_verified"] is True
    engine._llama_sentinel_path.write_text(json.dumps({"pid": 161, "port": port, "backend": "llamacpp"}))
    assert engine.manifest()["gpu_execution_verified"] is False
    engine._llama_sentinel_path.write_text(json.dumps({"pid": pid, "port": port, "backend": "llamacpp"}))
    assert engine.manifest()["gpu_execution_verified"] is False  # A new inference must re-establish proof.
    engine.manager.backend.handle = types.SimpleNamespace(base_url=handle.base_url, spawned_by_us=True)
    assert engine.manifest()["gpu_execution_verified"] is False
    assert engine.manifest()["gpu_execution_proof_source"] is None


def test_gpu_execution_rejects_wrong_binary_port_or_requested_layers(monkeypatch, tmp_path) -> None:
    service = _load_service(monkeypatch, tmp_path)
    engine = service.ENGINE
    engine.gpu_layers_requested = 99
    engine.manager = types.SimpleNamespace(
        backend=types.SimpleNamespace(handle=types.SimpleNamespace(base_url="http://127.0.0.1:8765/v1", spawned_by_us=True))
    )
    engine._llama_sentinel_path = tmp_path / "llamacpp_server.json"
    engine._llama_sentinel_path.write_text(json.dumps({"pid": 160, "port": 8765, "backend": "llamacpp"}))
    engine._proc_root = tmp_path / "proc"
    proc = engine._proc_root / "160"
    proc.mkdir(parents=True)
    (proc / "exe").write_bytes(b"other-binary")
    binary = tmp_path / "llama-server"
    binary.write_bytes(b"expected-binary")
    service.settings.LLAMA_CPP_BINARY = str(binary)
    (proc / "cmdline").write_bytes(
        b"/opt/llama/llama-server\0-m\0" + os.fsencode(engine.model_path)
        + b"\0--mmproj\0" + os.fsencode(engine.mmproj_path)
        + b"\0-ngl\00\0--port\08765\0"
    )
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(service.subprocess, "run", lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout="160\n"))
    engine._verify_gpu_execution_from_process()
    assert engine.manifest()["gpu_execution_verified"] is False
    service.settings.LLAMA_CPP_BINARY = str(proc / "exe")
    engine._verify_gpu_execution_from_process()
    assert engine.manifest()["gpu_execution_verified"] is False
    (proc / "cmdline").write_bytes((proc / "cmdline").read_bytes().replace(b"--port\08765", b"--port\08766").replace(b"-ngl\00", b"-ngl\099"))
    engine._verify_gpu_execution_from_process()
    assert engine.manifest()["gpu_execution_verified"] is False


def test_gpu_health_rejects_zero_offload_or_stale_log(monkeypatch, tmp_path) -> None:
    service = _load_service(monkeypatch, tmp_path)
    engine = service.ENGINE
    engine.gpu_layers_requested = 99
    engine._llama_log_path = tmp_path / "llamacpp_server.log"
    engine._llama_log_path.write_text(
        "load_tensors: offloaded 25/25 layers to GPU\n"
        "load_tensors: CUDA0 model buffer size = 1024.00 MiB\n",
        encoding="utf-8",
    )
    engine._llama_log_start = engine._llama_log_path.stat().st_size
    engine._verify_gpu_offload_from_log()
    assert engine.manifest()["gpu_offload_verified"] is False

    with engine._llama_log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("load_tensors: offloaded 0/25 layers to GPU\n")
        log_file.write("load_tensors: CPU_Mapped model buffer size = 1024.00 MiB\n")
    engine._verify_gpu_offload_from_log()
    assert engine.manifest()["gpu_offload_verified"] is False


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
