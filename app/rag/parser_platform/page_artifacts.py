from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from rag.parser_platform.errors import parser_error
from rag.parser_platform.surya_contract import CompletedSuryaManifest, SuryaPageArtifact


class PageArtifactStore(Protocol):
    def list_valid(self, *, source_hash: str, parser_fingerprint: str) -> dict[int, SuryaPageArtifact]: ...

    def put(self, artifact: SuryaPageArtifact) -> None: ...


class MemoryPageArtifactStore:
    def __init__(self):
        self._by_key: dict[str, SuryaPageArtifact] = {}

    def list_valid(self, *, source_hash: str, parser_fingerprint: str) -> dict[int, SuryaPageArtifact]:
        return {
            artifact.source_page: artifact
            for artifact in self._by_key.values()
            if artifact.source_hash == source_hash and artifact.parser_fingerprint == parser_fingerprint and artifact.status == "ok"
        }

    def put(self, artifact: SuryaPageArtifact) -> None:
        existing = self._by_key.get(artifact.artifact_key)
        if existing and existing.artifact_hash != artifact.artifact_hash:
            raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail="page artifact key collision")
        self._by_key.setdefault(artifact.artifact_key, artifact)


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


class FilePageArtifactStore:
    def __init__(self, repository: ParserArtifactRepository):
        self.repository = repository

    def _directory(self, source_hash: str, parser_fingerprint: str) -> Path:
        if not SAFE_ARTIFACT_NAME.fullmatch(source_hash) or not SAFE_ARTIFACT_NAME.fullmatch(parser_fingerprint):
            raise ValueError("unsafe page artifact identity")
        return self.repository.root / "pages" / source_hash / parser_fingerprint

    def list_valid(self, *, source_hash: str, parser_fingerprint: str) -> dict[int, SuryaPageArtifact]:
        directory = self._directory(source_hash, parser_fingerprint)
        if not directory.exists():
            return {}
        result = {}
        for path in sorted(directory.glob("*.json")):
            try:
                artifact = SuryaPageArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail=f"invalid stored page artifact {path.name}: {error}") from error
            if artifact.source_hash == source_hash and artifact.parser_fingerprint == parser_fingerprint and artifact.status == "ok":
                result[artifact.source_page] = artifact
        return result

    def put(self, artifact: SuryaPageArtifact) -> None:
        directory = self._directory(artifact.source_hash, artifact.parser_fingerprint)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{artifact.source_page:06d}.json"
        if target.exists():
            existing = SuryaPageArtifact.model_validate_json(target.read_text(encoding="utf-8"))
            if existing.artifact_hash != artifact.artifact_hash:
                raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail="page artifact collision")
            return
        temporary = directory / f".{artifact.source_page:06d}.{os.getpid()}.tmp"
        temporary.write_text(artifact.model_dump_json(), encoding="utf-8")
        os.replace(temporary, target)


def close_page_barrier(
    *,
    expected_page_count: int,
    stored_pages: Iterable[SuryaPageArtifact],
    reused_page_count: int,
) -> CompletedSuryaManifest:
    pages = tuple(sorted(stored_pages, key=lambda page: page.source_page))
    try:
        return CompletedSuryaManifest(
            expected_page_count=expected_page_count,
            pages=pages,
            reused_page_count=reused_page_count,
        )
    except ValueError as error:
        raise parser_error("PARSER_SURYA_INVALID_OUTPUT", detail=str(error)) from error
