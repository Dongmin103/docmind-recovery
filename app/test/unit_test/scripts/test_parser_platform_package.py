from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_compose_is_kordoc_only_and_enabled_by_default() -> None:
    compose = yaml.safe_load((ROOT / "docker" / "docker-compose-parser-platform.yml").read_text(encoding="utf-8"))
    ragflow = compose["services"]["ragflow-cpu"]
    assert set(compose["services"]) == {"ragflow-cpu", "kordoc-parser"}
    assert ragflow["environment"]["PARSER_PLATFORM_ENABLED"].endswith(":-1}")
    assert ragflow["environment"]["PARSER_PLATFORM_INTEGRATION_READY"].endswith(":-1}")
    assert ragflow["environment"]["TE_RUN_MODE"].endswith(":-0}")
    assert compose["networks"]["parser_internal"]["internal"] is True
    assert "parser_platform_artifacts" in compose["volumes"]


def test_compose_wires_all_formats_and_pdf_cap_to_kordoc() -> None:
    compose = yaml.safe_load((ROOT / "docker" / "docker-compose-parser-platform.yml").read_text(encoding="utf-8"))
    app = compose["services"]["ragflow-cpu"]
    parser = compose["services"]["kordoc-parser"]
    for format_name in ("PDF", "DOCX", "EXCEL", "PPTX", "HWP"):
        assert app["environment"][f"PARSER_PLATFORM_KORDOC_{format_name}_ENABLED"].endswith(":-1}")
    assert app["environment"]["PARSER_PLATFORM_HWP_ENABLED"].endswith(":-1}")
    assert parser["environment"]["KORDOC_MAX_PDF_PAGES"] == app["environment"]["PARSER_PLATFORM_MAX_PDF_PAGES"]
    assert parser["environment"]["KORDOC_MAX_SOURCE_BYTES"] == app["environment"]["PARSER_PLATFORM_KORDOC_MAX_SOURCE_BYTES"]
    assert parser["read_only"] is True and parser["cap_drop"] == ["ALL"]
    assert "ports" not in parser and list(parser["networks"]) == ["parser_internal"]
    assert parser["platform"] == "linux/amd64"
    assert any("/models/kordoc:ro" in volume for volume in parser["volumes"])


def test_public_images_do_not_copy_private_evidence_or_model_weights() -> None:
    dockerfiles = [ROOT / "Dockerfile", ROOT / "parser_services" / "kordoc" / "Containerfile"]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in dockerfiles)
    assert "COPY .omx" not in combined
    assert "COPY output" not in combined
    assert "COPY --from=surya_models" not in combined
    assert "docmind-goldset-dev.jsonl" not in combined
