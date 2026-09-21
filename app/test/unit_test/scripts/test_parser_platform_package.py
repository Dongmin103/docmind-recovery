from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_compose_defaults_off_and_parser_network_is_internal() -> None:
    compose = yaml.safe_load((ROOT / "docker" / "docker-compose-parser-platform.yml").read_text(encoding="utf-8"))
    ragflow = compose["services"]["ragflow-cpu"]
    assert ragflow["environment"]["PARSER_PLATFORM_ENABLED"].endswith(":-0}")
    assert ragflow["environment"]["PARSER_PLATFORM_INTEGRATION_READY"].endswith(":-0}")
    assert ragflow["environment"]["TE_RUN_MODE"].endswith(":-0}")
    assert compose["networks"]["parser_internal"]["internal"] is True
    assert "parser_platform_artifacts" in compose["volumes"]


def test_cpu_and_docling_profiles_are_single_concurrency_read_only_and_unpublished() -> None:
    compose = yaml.safe_load((ROOT / "docker" / "docker-compose-parser-platform.yml").read_text(encoding="utf-8"))
    cpu = compose["services"]["surya-parser-cpu"]
    docling = compose["services"]["docling-office-parser"]
    rhwp = compose["services"]["rhwp-parser"]
    assert cpu["profiles"] == ["parser-platform-cpu"]
    assert cpu["environment"]["SURYA_INFERENCE_PARALLEL"] == "1"
    assert cpu["read_only"] is True and cpu["cap_drop"] == ["ALL"]
    assert cpu["mem_limit"].endswith(":-8g}") and cpu["cpus"].endswith(":-8}")
    assert "ports" not in cpu and list(cpu["networks"]) == ["parser_internal"]
    assert cpu["platform"] == "linux/amd64"
    assert docling["profiles"] == ["parser-platform-office"]
    assert docling["platform"] == "linux/amd64"
    assert docling["read_only"] is True and docling["cap_drop"] == ["ALL"]
    assert "ports" not in docling and list(docling["networks"]) == ["parser_internal"]
    assert rhwp["profiles"] == ["parser-platform-hwp"]
    assert rhwp["platform"] == "linux/amd64"
    assert rhwp["read_only"] is True and rhwp["cap_drop"] == ["ALL"]


def test_gpu_profile_is_fail_closed_until_pinned_nvidia_runtime_exists() -> None:
    compose = yaml.safe_load((ROOT / "docker" / "docker-compose-parser-platform.yml").read_text(encoding="utf-8"))
    gpu = compose["services"]["surya-parser-gpu"]
    assert gpu["profiles"] == ["parser-platform-gpu"]
    assert "gpu-runtime-not-configured" in gpu["image"]
    assert gpu["environment"]["SURYA_INFERENCE_BACKEND"] == "vllm"
    devices = gpu["deploy"]["resources"]["reservations"]["devices"]
    assert devices == [{"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}]


def test_office_image_keeps_only_real_native_backend_dependencies() -> None:
    project = (ROOT / "parser_services" / "docling_office" / "pyproject.toml").read_text(encoding="utf-8")
    service = (ROOT / "parser_services" / "docling_office" / "service.py").read_text(encoding="utf-8")
    assert "docling-slim[format-office]==2.115.0" in project
    assert "pypdfium2==5.12.1" in project
    assert "scipy==1.18.0" in project
    assert "docling-parse" not in project
    assert "docling-ibm-models" not in project
    assert "rtree" not in project
    assert "DocumentConverter" not in service
    assert "SimplePipeline" not in service
    assert "MsWordDocumentBackend" in service
    assert "MsExcelDocumentBackend" in service
    assert "MsPowerpointDocumentBackend" in service


def test_docmind_image_contains_sandboxed_svg_renderer_and_artifact_directory() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "librsvg2-bin" in dockerfile
    assert "/ragflow/parser-platform-artifacts" in dockerfile
    assert "/ragflow/scripts" not in dockerfile
    assert "COPY scripts scripts" in dockerfile


def test_public_images_do_not_copy_private_evidence_or_model_weights() -> None:
    dockerfiles = [
        ROOT / "Dockerfile",
        ROOT / "parser_services" / "surya" / "Containerfile.cpu-amd64",
        ROOT / "parser_services" / "docling_office" / "Containerfile",
        ROOT / "parser_services" / "rhwp" / "Containerfile",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in dockerfiles)
    assert "COPY .omx" not in combined
    assert "COPY output" not in combined
    assert "COPY --from=surya_models" not in combined
    assert "docmind-goldset-dev.jsonl" not in combined


def test_parser_http_admission_is_strictly_serial() -> None:
    for service in ("surya", "docling_office", "rhwp"):
        source = (ROOT / "parser_services" / service / "service.py").read_text(encoding="utf-8")
        assert "HTTPServer((host, port), Handler)" in source
        assert "ThreadingHTTPServer" not in source
