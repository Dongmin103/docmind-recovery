from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCRIPT_PATH = REPOSITORY_ROOT / "app" / "scripts" / "verify_removed_runtime.py"
SPEC = importlib.util.spec_from_file_location("verify_removed_runtime", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_repository_has_no_removed_runtime_references():
    assert MODULE.scan_repository(REPOSITORY_ROOT) == []


def test_product_configuration_is_rejected(tmp_path):
    compose_path = tmp_path / "app" / "docker" / "compose.yml"
    compose_path.parent.mkdir(parents=True)
    retired_name = "open" + "viking"
    compose_path.write_text(f"services:\n  {retired_name}:\n    image: example.invalid/retired\n", encoding="utf-8")

    findings = MODULE.scan_paths(tmp_path, ["app/docker/compose.yml"])

    assert [(finding.path, finding.rule) for finding in findings] == [("app/docker/compose.yml", "retired integration name")]


def test_protocol_and_environment_references_are_rejected_everywhere(tmp_path):
    documentation_path = tmp_path / "docs" / "current.md"
    documentation_example_path = tmp_path / "docs" / "example.md"
    environment_path = tmp_path / "deployment.env.example"
    package_path = tmp_path / "app" / "requirements.txt"
    documentation_path.parent.mkdir()
    package_path.parent.mkdir()
    documentation_path.write_text("viking" + "://catalog/example", encoding="utf-8")
    documentation_example_path.write_text("```yaml\nservice: " + "open" + "-viking\n```\n", encoding="utf-8")
    environment_path.write_text(("OPEN" + "_VIKING") + "_URL=https://example.invalid\n", encoding="utf-8")
    package_path.write_text("open" + "-viking==1.0\n", encoding="utf-8")

    findings = MODULE.scan_paths(
        tmp_path,
        ["app/requirements.txt", "deployment.env.example", "docs/current.md", "docs/example.md"],
    )

    assert [(finding.path, finding.rule) for finding in findings] == [
        ("app/requirements.txt", "retired integration name"),
        ("deployment.env.example", "retired environment prefix"),
        ("docs/current.md", "retired URI scheme"),
        ("docs/example.md", "retired integration name"),
    ]


def test_only_exact_recovery_evidence_path_is_allowed(tmp_path):
    retired_uri = "viking" + "://catalog/example"
    allowed_path = tmp_path / "checkpoint.json"
    nested_path = tmp_path / "nested" / "checkpoint.json"
    nested_path.parent.mkdir()
    allowed_path.write_text(retired_uri, encoding="utf-8")
    nested_path.write_text(retired_uri, encoding="utf-8")

    assert MODULE.scan_paths(tmp_path, ["checkpoint.json"]) == []
    findings = MODULE.scan_paths(tmp_path, ["nested/checkpoint.json"])
    assert [(finding.path, finding.rule) for finding in findings] == [("nested/checkpoint.json", "retired URI scheme")]
