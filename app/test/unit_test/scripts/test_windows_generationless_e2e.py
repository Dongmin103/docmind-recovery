from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
REPO = ROOT.parent


def _overlay() -> dict:
    return yaml.safe_load(
        (ROOT / "docker" / "docker-compose-windows-dev-generationless-e2e.yml").read_text(encoding="utf-8")
    )


def test_generationless_overlay_requires_jina_and_blanks_answer_credentials() -> None:
    overlay = _overlay()
    gate = overlay["services"]["model-secret-gate"]
    command = "\n".join(gate["command"])
    assert "Jina API credential is missing" in command
    assert 'cat "$$JINA_API_KEY_FILE"' in command
    assert 'jina_api_key="$$JINA_API_KEY"' in command
    assert "DOCMIND_E2E_BOOTSTRAP_ONLY must be 0 or 1" in command
    assert '"$$DOCMIND_E2E_BOOTSTRAP_ONLY" = 0' in command
    assert "answer generation must remain disabled" in command
    assert gate["environment"]["DOCMIND_GENERATOR_MODEL"] == ""
    assert gate["environment"]["DASHSCOPE_API_KEY"] == ""

    app = overlay["services"]["ragflow-cpu"]
    assert app["environment"]["DOCMIND_GENERATOR_MODEL"] == ""
    assert app["environment"]["DASHSCOPE_API_KEY"] == ""
    assert app["environment"]["JINA_API_KEY_FILE"] == "${DOCMIND_E2E_JINA_API_KEY_FILE:-}"
    assert app["environment"]["API_PROXY_SCHEME"] == "python"
    assert app["environment"]["DOCMIND_E2E_BOOTSTRAP_ONLY"] == "${DOCMIND_E2E_BOOTSTRAP_ONLY:-0}"
    assert app["environment"]["DOCMIND_GENERATIONLESS_E2E_ENABLED"] == "1"
    assert app["environment"]["TEI_MODEL"] == "BAAI/bge-m3"
    assert "tei-cpu" in app["environment"]["COMPOSE_PROFILES"]
    assert app["environment"]["DOCMIND_SHARED_WORKSPACE_ENABLED"] == "1"
    assert app["environment"]["DOCMIND_CATALOG_DB_PRIMARY_ENABLED"] == "1"
    assert app["environment"]["PARSER_PLATFORM_ENABLED"] == "1"
    assert app["environment"]["PARSER_PLATFORM_INTEGRATION_READY"] == "1"
    assert any("docmind_generationless_e2e_seed.py" in volume for volume in app["volumes"])


def test_generationless_kordoc_parser_is_private_and_required() -> None:
    overlay = _overlay()
    assert set(overlay["services"]) == {"model-secret-gate", "ragflow-cpu", "kordoc-parser"}
    parser = overlay["services"]["kordoc-parser"]
    app = overlay["services"]["ragflow-cpu"]
    assert parser["read_only"] is True and parser["cap_drop"] == ["ALL"]
    assert "ports" not in parser
    assert list(parser["networks"]) == ["docmind-parser-internal"]
    assert overlay["networks"]["docmind-parser-internal"]["internal"] is True
    assert "docmind-parser-internal" in app["networks"]
    assert app["depends_on"]["kordoc-parser"]["condition"] == "service_healthy"
    assert app["environment"]["PARSER_PLATFORM_KORDOC_URL"] == "http://kordoc-parser:8095"
    for format_name in ("PDF", "DOCX", "EXCEL", "PPTX", "HWP"):
        assert app["environment"][f"PARSER_PLATFORM_KORDOC_{format_name}_ENABLED"] == "1"


def test_readiness_probe_never_accepts_a_credential_argument_or_prints_paths() -> None:
    probe = (REPO / "tools" / "windows" / "Test-DocMindGenerationlessE2EReadiness.ps1").read_text(encoding="utf-8")
    parameter_block = probe.split(")", 1)[0]
    assert "JinaApiKey" not in parameter_block
    assert "JINA_API_KEY_NOT_AVAILABLE_TO_E2E_PROCESS" in probe
    assert "answer_generation_required = $false" in probe
    assert "ConvertTo-Json" in probe


def test_seed_script_accepts_no_physical_root_plaintext_or_credential() -> None:
    seed = (ROOT / "scripts" / "docmind_generationless_e2e_seed.py").read_text(encoding="utf-8")
    assert "DOCMIND_E2E_RELATIVE_PATH" in seed
    assert "SOURCE_ROOT" not in seed
    assert "JINA_API_KEY" not in seed
    assert "shared_secret" not in seed
    assert "read_bytes" not in seed
