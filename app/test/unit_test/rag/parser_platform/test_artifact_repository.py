from __future__ import annotations

import json

import pytest

from rag.parser_platform.artifact_repository import ParserArtifactRepository


def test_run_artifact_is_written_and_only_its_run_is_removed(tmp_path) -> None:
    repository = ParserArtifactRepository(tmp_path)
    first = repository.write_json(parse_run_id="run-a", name="kordoc-raw", payload={"text": "가"})
    second = repository.write_json(parse_run_id="run-b", name="kordoc-raw", payload={"text": "나"})

    assert first == "artifact://runs/run-a/kordoc-raw.json"
    assert json.loads((tmp_path / "runs" / "run-a" / "kordoc-raw.json").read_text(encoding="utf-8")) == {"text": "가"}

    repository.remove_run_ref(first)

    assert not (tmp_path / "runs" / "run-a").exists()
    assert (tmp_path / "runs" / "run-b" / "kordoc-raw.json").exists()
    with pytest.raises(ValueError, match="unsupported"):
        repository.remove_run_ref("file:///tmp/run-a")
    assert second == "artifact://runs/run-b/kordoc-raw.json"


def test_page_artifact_cleanup_is_scoped_to_source_and_parser(tmp_path) -> None:
    repository = ParserArtifactRepository(tmp_path)
    first = tmp_path / "pages" / ("a" * 64) / ("b" * 64)
    other = tmp_path / "pages" / ("a" * 64) / ("c" * 64)
    first.mkdir(parents=True)
    other.mkdir(parents=True)
    (first / "000001.json").write_text("{}", encoding="utf-8")
    (other / "000001.json").write_text("{}", encoding="utf-8")

    repository.remove_page_artifacts("a" * 64, "b" * 64)

    assert not first.exists()
    assert other.exists()
