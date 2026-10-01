"""Deployment boundaries for the opt-in plaintext preview runtime."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_preview_processor_has_private_network_and_hard_resource_limits():
    config = yaml.safe_load((ROOT / "docker/docker-compose-windows-dev-preview.yml").read_text())
    processor = config["services"]["docmind-preview-processor"]
    assert "ports" not in processor
    assert processor["read_only"] is True
    assert processor["cap_drop"] == ["ALL"]
    assert processor["security_opt"] == ["no-new-privileges:true"]
    assert processor["mem_limit"] == processor["memswap_limit"] == "256m"
    assert processor["cpus"] == 0.5
    assert processor["pids_limit"] == 64
    assert config["networks"]["docmind-preview-internal"]["internal"] is True


def test_originals_and_converter_scratch_share_one_bounded_tmpfs():
    config = yaml.safe_load((ROOT / "docker/docker-compose-windows-dev-preview.yml").read_text())
    volume = config["volumes"]["preview_memory_128m"]["driver_opts"]
    assert volume["type"] == volume["device"] == "tmpfs"
    assert "size=134217728" in volume["o"]
    for flag in ("noexec", "nosuid", "nodev", "mode=0700"):
        assert flag in volume["o"]
    for name in ("ragflow-cpu", "docmind-preview-processor"):
        service = config["services"][name]
        assert service["volumes"] == ["preview_memory_128m:/run/docmind-previews"]
        assert service["environment"]["DOCMIND_PREVIEW_ROOT"] == "/run/docmind-previews"
        assert "tmpfs" not in service
    processor = config["services"]["docmind-preview-processor"]
    for key in ("HOME", "TMPDIR", "XDG_CACHE_HOME"):
        assert processor["environment"][key].startswith("/run/docmind-previews/")


def test_preview_overlay_does_not_replace_existing_storage_or_ingestion():
    config = yaml.safe_load((ROOT / "docker/docker-compose-windows-dev-preview.yml").read_text())
    assert set(config["services"]) == {"ragflow-cpu", "docmind-preview-processor"}
    app = config["services"]["ragflow-cpu"]
    assert "STORAGE_IMPL" not in app["environment"]
    assert not any(key.startswith("PARSER_PLATFORM_") for key in app["environment"])
    assert "depends_on" not in app  # Processor failure must not take search down.
