"""Raw parser artifact storage independent of retired page engines."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

SAFE_ARTIFACT_NAME = re.compile(r"^[0-9A-Za-z_.-]+$")


class ParserArtifactRepository:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(self, *, parse_run_id: str, name: str, payload: dict) -> str:
        if not SAFE_ARTIFACT_NAME.fullmatch(parse_run_id) or not SAFE_ARTIFACT_NAME.fullmatch(name):
            raise ValueError("unsafe parser artifact identity")
        directory = self.root / "runs" / parse_run_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{name}.json"
        temporary = directory / f".{name}.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return f"artifact://runs/{parse_run_id}/{name}.json"

    def run_ref(self, parse_run_id: str) -> str:
        if not SAFE_ARTIFACT_NAME.fullmatch(parse_run_id):
            raise ValueError("unsafe parser run identity")
        return f"artifact://runs/{parse_run_id}"

    def remove_run_ref(self, artifact_ref: str) -> None:
        prefix = "artifact://runs/"
        if not artifact_ref.startswith(prefix):
            raise ValueError("unsupported parser artifact reference")
        parse_run_id = artifact_ref.removeprefix(prefix).split("/", 1)[0]
        if not SAFE_ARTIFACT_NAME.fullmatch(parse_run_id):
            raise ValueError("unsafe parser run identity")
        run_directory = (self.root / "runs" / parse_run_id).resolve()
        if not run_directory.is_relative_to(self.root / "runs"):
            raise ValueError("parser artifact path escaped repository root")
        if run_directory.exists():
            shutil.rmtree(run_directory)

    def remove_page_artifacts(self, source_hash: str, parser_fingerprint: str) -> None:
        if not SAFE_ARTIFACT_NAME.fullmatch(source_hash) or not SAFE_ARTIFACT_NAME.fullmatch(parser_fingerprint):
            raise ValueError("unsafe page artifact identity")
        page_directory = (self.root / "pages" / source_hash / parser_fingerprint).resolve()
        if not page_directory.is_relative_to(self.root / "pages"):
            raise ValueError("page artifact path escaped repository root")
        if page_directory.exists():
            shutil.rmtree(page_directory)
