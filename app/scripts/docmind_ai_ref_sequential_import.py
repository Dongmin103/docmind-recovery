#!/usr/bin/env python3
"""Import the physical ai-ref PDF tree into DocMind one document at a time."""

from __future__ import annotations

import argparse
import collections
import fcntl
import hashlib
import hmac
import json
import re
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

TERMINAL_SUCCESS = {"INDEXED"}
TERMINAL_FAILURE = {"FAILED", "CANCELLED"}
EXPECTED_CHUNKER_VERSION = "2.95.0"
RECOVERABLE_PDF_PREAMBLE = re.compile(
    rb"\A\{!-- ra:[0-9A-Fa-f]{8,128} --\}(?:\r\n|\n|\r)"
)
LEGACY_ACCEPTED_CHUNKER_VERSIONS = frozenset({"2.93.6", "2.94.2"})
QUALITY_GATE_VERSION = "5"
QUALITY_RETRY_POLICY_VERSION = "short-blank-page-coalesce-v1"
ROUTING_BUDGET_POLICY_VERSION = "pdf-page-batch-resume-v3"
AUTO_RETRYABLE_PARSER_ERRORS = frozenset(
    {
        "DOCMIND_INDEX_FAILED",
        "PARSER_SURYA_PAGE_LIMIT_EXCEEDED",
        "PARSER_SURYA_TIMEOUT",
        "PARSER_SURYA_UNAVAILABLE",
        "SURYA_PDF_DEADLINE_EXCEEDED",
        "SURYA_PDF_HEARTBEAT_TIMEOUT",
    }
)
SURYA_CONTAINER = "docmind-surya-parser-cpu-1"
PENDING_REGISTRATION_REQUEUE_SECONDS = 60.0
DEFAULT_CONTAINERS = (
    "docmind-ragflow-cpu-1",
    "docmind-bge-m3",
    SURYA_CONTAINER,
    "docmind-docling-pdf-parser-1",
    "docmind-openviking",
    "docmind-minio-1",
    "docmind-es01-1",
    "docmind-mysql-1",
    "docmind-redis-1",
)


class ImportStopped(RuntimeError):
    pass


class RegistrationDeferred(ImportStopped):
    def __init__(self, relative: str, registration: dict[str, Any]):
        super().__init__(relative)
        self.relative = relative
        self.registration = registration


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def event(name: str, **fields: Any) -> None:
    print(json.dumps({"at": utc_now(), "event": name, **fields}, ensure_ascii=False, sort_keys=True), flush=True)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class SequentialImporter:
    def __init__(self, args: argparse.Namespace):
        self.source = args.source.resolve()
        self.queue_path = args.queue.resolve()
        self.runtime_review_path = args.runtime_review_queue.resolve()
        self.base_url = args.base_url.rstrip("/")
        self.cookie = args.cookie.resolve()
        self.state_path = args.state.resolve()
        self.poll_seconds = args.poll_seconds
        self.timeout_seconds = args.timeout_seconds
        self.warning_seconds = args.warning_seconds
        self.swap_limit_kib = args.swap_limit_gib * 1024 * 1024
        self.expected_chunker_version = args.expected_chunker_version
        self.queue_disposition = args.queue_disposition
        self.max_surya_pages = args.max_surya_pages
        self.max_parser_retries = args.max_parser_retries
        self.max_consecutive_deferred = args.max_consecutive_deferred
        self.surya_recycle_memory_percent = args.surya_recycle_memory_percent
        self.consecutive_deferred = 0
        self.swap_high_samples = 0
        self.baseline_swap_used_kib = 0
        self.baseline_restarts: dict[str, int] = {}
        self.baseline_surya_watchdog_events = 0
        self.catalog_version_id: str | None = None
        self.completed = 0
        self.skipped = 0
        self.failed = 0
        self.current: str | None = None
        self.total = 0
        self.eligible = 0
        self.deferred_review = 0
        self.out_of_scope = 0
        self.deferred_runtime = 0
        self.preflight_manifest_sha256: str | None = None
        self.preflight_paths: set[str] = set()
        self.queue_max_surya_pages = 0
        self.runtime_surya_page_cap: int | None = None
        self.runtime_deferred_records: dict[str, dict[str, Any]] = {}

    def write_state(self, status: str, **extra: Any) -> None:
        atomic_json(
            self.state_path,
            {
                "updated_at": utc_now(),
                "status": status,
                "source": str(self.source),
                "queue": str(self.queue_path),
                "total_pdf": self.total,
                "eligible_pdf": self.eligible,
                "queue_disposition": self.queue_disposition,
                "max_surya_pages": self.max_surya_pages,
                "deferred_review": self.deferred_review,
                "out_of_scope": self.out_of_scope,
                "deferred_runtime": self.deferred_runtime,
                "total_deferred_review": self.deferred_review + self.deferred_runtime,
                "runtime_review_queue": str(self.runtime_review_path),
                "preflight_manifest_sha256": self.preflight_manifest_sha256,
                "queue_max_surya_pages": self.queue_max_surya_pages,
                "runtime_surya_page_cap": self.runtime_surya_page_cap,
                "completed": self.completed,
                "skipped_existing": self.skipped,
                "failed": self.failed,
                "consecutive_deferred": self.consecutive_deferred,
                "current_relative_path": self.current,
                **extra,
            },
        )

    def refresh_session(self) -> None:
        self.cookie.parent.mkdir(parents=True, exist_ok=True)
        command = [
            "curl",
            "-fsS",
            "-o",
            "/dev/null",
            "-c",
            str(self.cookie),
            "-X",
            "POST",
            f"{self.base_url}/api/v1/docmind/shared-session",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ImportStopped(f"shared session failed: curl exit {result.returncode}")
        os.chmod(self.cookie, 0o600)

    def curl_json(self, endpoint: str, *, method: str = "GET", arguments: list[str] | None = None, timeout: int = 300) -> dict[str, Any]:
        command = [
            "curl",
            "-sS",
            "--fail-with-body",
            "--connect-timeout",
            "10",
            "--max-time",
            str(timeout),
            "-b",
            str(self.cookie),
            "-X",
            method,
            f"{self.base_url}{endpoint}",
        ]
        if arguments:
            command.extend(arguments)
        for attempt in range(2):
            result = subprocess.run(command, capture_output=True, timeout=timeout + 15)
            if result.returncode == 0:
                try:
                    envelope = json.loads(result.stdout)
                except json.JSONDecodeError as error:
                    raise ImportStopped("DocMind API returned non-JSON data") from error
                if envelope.get("code") not in (0, "0", None):
                    raise ImportStopped(f"DocMind API error: {envelope.get('message') or envelope.get('code')}")
                data = envelope.get("data")
                if not isinstance(data, dict):
                    raise ImportStopped("DocMind API response did not contain an object")
                return data
            if attempt == 0:
                self.refresh_session()
        stderr = result.stderr.decode(errors="replace").strip()
        raise ImportStopped(f"curl failed: exit={result.returncode} detail={stderr[-240:]}")

    def pdf_paths(self) -> list[tuple[Path, str]]:
        found: list[tuple[Path, str]] = []
        for directory, dirnames, filenames in os.walk(self.source):
            dirnames.sort(key=str.casefold)
            for filename in sorted(filenames, key=str.casefold):
                if Path(filename).suffix.casefold() != ".pdf":
                    continue
                absolute = Path(directory) / filename
                relative = PurePosixPath(*absolute.relative_to(self.source).parts).as_posix()
                found.append((absolute, relative))
        return found

    @staticmethod
    def file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def curl_file_form(path: Path) -> str:
        quoted_path = str(path).replace("\\", "\\\\").replace('"', '\\"')
        return f'file=@"{quoted_path}";type=application/pdf'

    def load_runtime_review_queue(self) -> None:
        if not self.runtime_review_path.exists():
            return
        try:
            payload = json.loads(self.runtime_review_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise ImportStopped(
                f"runtime review queue is unreadable: {self.runtime_review_path}"
            ) from error
        if payload.get("schema_version") != "docmind-ai-ref-runtime-review-v1":
            raise ImportStopped("runtime review queue schema is not supported")
        self.preflight_manifest_sha256 = (
            str(payload.get("source_manifest_sha256") or "").strip() or None
        )
        records = payload.get("files")
        if not isinstance(records, list) or payload.get("count") != len(records):
            raise ImportStopped("runtime review queue count does not match its file list")
        mapped: dict[str, dict[str, Any]] = {}
        retryable_stale_quality: list[str] = []
        retryable_stale_routing: list[str] = []
        for record in records:
            if not isinstance(record, dict):
                raise ImportStopped("runtime review queue contains a non-object entry")
            relative = str(record.get("relative_path") or "")
            if not relative or relative in mapped:
                raise ImportStopped(
                    "runtime review queue contains a duplicate or empty path"
                )
            quality_gate = record.get("quality_gate") or {}
            error_code = str(record.get("error_code") or "")
            if error_code == "CHUNK_QUALITY_GATE_FAILED":
                quality_issues = set(quality_gate.get("issues") or [])
                stale_gate = (
                    quality_gate.get("quality_gate_version") != QUALITY_GATE_VERSION
                )
                stale_short_policy = (
                    quality_issues == {"short_lt48"}
                    and record.get("chunk_quality_policy_version")
                    != QUALITY_RETRY_POLICY_VERSION
                )
                if stale_gate or stale_short_policy:
                    retryable_stale_quality.append(relative)
                    continue
            stale_inflight_failure = (
                error_code == "DOCMIND_INDEX_FAILED"
                and record.get("parser_phase") == "PARSING_SURYA"
            )
            if (
                (error_code in AUTO_RETRYABLE_PARSER_ERRORS or stale_inflight_failure)
                and record.get("routing_budget_policy_version") != ROUTING_BUDGET_POLICY_VERSION
            ):
                retryable_stale_routing.append(relative)
                continue
            mapped[relative] = record
        self.runtime_deferred_records = mapped
        self.deferred_runtime = len(mapped)
        if retryable_stale_quality or retryable_stale_routing:
            self.persist_runtime_review_queue()
        if retryable_stale_quality:
            event(
                "stale_quality_deferrals_requeued",
                count=len(retryable_stale_quality),
            )
        if retryable_stale_routing:
            event(
                "stale_routing_deferrals_requeued",
                count=len(retryable_stale_routing),
                policy_version=ROUTING_BUDGET_POLICY_VERSION,
            )

    def persist_runtime_review_queue(self) -> None:
        atomic_json(
            self.runtime_review_path,
            {
                "schema_version": "docmind-ai-ref-runtime-review-v1",
                "updated_at": utc_now(),
                "source_queue": str(self.queue_path),
                "source_manifest_sha256": self.preflight_manifest_sha256,
                "count": self.deferred_runtime,
                "files": sorted(
                    self.runtime_deferred_records.values(),
                    key=lambda record: str(record["relative_path"]).casefold(),
                ),
            },
        )

    @staticmethod
    def registration_error_code(registration: dict[str, Any]) -> str:
        parser_run = registration.get("parser_run")
        parser_error = (
            parser_run.get("error_code") if isinstance(parser_run, dict) else None
        )
        return str(
            parser_error
            or registration.get("error_code")
            or registration.get("blocker_code")
            or "unknown"
        )

    def has_recoverable_pdf_preamble(self, relative: str) -> bool:
        relative_path = PurePosixPath(relative)
        if relative_path.suffix.casefold() != ".pdf":
            return False
        candidate = (self.source / Path(*relative_path.parts)).resolve()
        try:
            candidate.relative_to(self.source)
        except ValueError:
            return False
        try:
            with candidate.open("rb") as handle:
                prefix = handle.read(256)
        except OSError:
            return False
        match = RECOVERABLE_PDF_PREAMBLE.match(prefix)
        return (
            match is not None
            and prefix[match.end() :].startswith(b"%PDF-")
        )

    def registration_is_auto_retryable(
        self, registration: dict[str, Any], relative: str
    ) -> bool:
        error_code = self.registration_error_code(registration)
        return error_code in AUTO_RETRYABLE_PARSER_ERRORS or (
            error_code == "PARSER_SOURCE_TYPE_MISMATCH"
            and self.has_recoverable_pdf_preamble(relative)
        )

    @staticmethod
    def registration_retry_depth(
        registration: dict[str, Any], registrations: list[dict[str, Any]]
    ) -> int:
        by_id = {
            str(item.get("registration_id") or ""): item
            for item in registrations
            if item.get("registration_id")
        }
        current = registration
        visited: set[str] = set()
        depth = 0
        while retry_of_id := str(current.get("retry_of_id") or ""):
            if retry_of_id in visited:
                raise ImportStopped("registration retry chain contains a cycle")
            visited.add(retry_of_id)
            depth += 1
            parent = by_id.get(retry_of_id)
            if parent is None:
                break
            current = parent
        return depth

    def recover_retryable_registration(
        self, registration: dict[str, Any], relative: str
    ) -> dict[str, Any]:
        current = registration
        while str(current.get("state") or "") in TERMINAL_FAILURE:
            error_code = self.registration_error_code(current)
            if not self.registration_is_auto_retryable(current, relative):
                raise RegistrationDeferred(relative, current)
            retry_depth = self.registration_retry_depth(current, self.registrations())
            if retry_depth >= self.max_parser_retries:
                event(
                    "registration_auto_retry_exhausted",
                    relative_path=relative,
                    error_code=error_code,
                    retry_depth=retry_depth,
                    max_parser_retries=self.max_parser_retries,
                )
                raise RegistrationDeferred(relative, current)
            event(
                "registration_auto_retry_started",
                relative_path=relative,
                error_code=error_code,
                retry_number=retry_depth + 1,
                max_parser_retries=self.max_parser_retries,
            )
            try:
                current = self.reprocess_stale_registration(current, relative)
            except RegistrationDeferred as deferred:
                current = deferred.registration
                event(
                    "registration_auto_retry_failed",
                    relative_path=relative,
                    error_code=self.registration_error_code(current),
                    retry_number=retry_depth + 1,
                )
                self.recycle_surya_if_needed()
                self.resource_gate()
                continue
            event(
                "registration_auto_retry_completed",
                relative_path=relative,
                retry_number=retry_depth + 1,
            )
        return current

    def defer_registration(
        self,
        relative: str,
        queue_record: dict[str, Any],
        registration: dict[str, Any],
    ) -> None:
        parser_run = registration.get("parser_run")
        parser_run = parser_run if isinstance(parser_run, dict) else {}
        error_code = self.registration_error_code(registration)
        self.runtime_deferred_records[relative] = {
            "relative_path": relative,
            "sha256": queue_record.get("sha256"),
            "preflight_route": queue_record.get("route"),
            "preflight_page_count": queue_record.get("page_count"),
            "preflight_surya_page_count": queue_record.get("surya_page_count"),
            "document_id": registration.get("document_id"),
            "registration_id": registration.get("registration_id"),
            "error_code": error_code,
            "parser_phase": parser_run.get("phase"),
            "runtime_expected_pages": parser_run.get("expected_pages"),
            "runtime_reused_pages": parser_run.get("reused_pages"),
            "routing_budget_policy_version": ROUTING_BUDGET_POLICY_VERSION,
            "chunk_quality_policy_version": QUALITY_RETRY_POLICY_VERSION,
            "quality_gate": registration.get("quality_gate"),
            "deferred_at": utc_now(),
        }
        self.deferred_runtime = len(self.runtime_deferred_records)
        self.consecutive_deferred += 1
        self.persist_runtime_review_queue()
        event(
            "registration_deferred",
            relative_path=relative,
            error_code=error_code,
            runtime_expected_pages=parser_run.get("expected_pages"),
            preflight_surya_page_count=queue_record.get("surya_page_count"),
            quality_gate=registration.get("quality_gate"),
            consecutive_deferred=self.consecutive_deferred,
        )
        self.write_state("RUNNING")
        if self.consecutive_deferred >= self.max_consecutive_deferred:
            raise ImportStopped(
                "consecutive document deferrals reached the safety limit: "
                f"{self.consecutive_deferred}"
            )

    def queued_pdf_paths(self) -> list[tuple[Path, str, dict[str, Any]]]:
        if not self.queue_path.is_file():
            raise ImportStopped(f"preflight queue does not exist: {self.queue_path}")
        try:
            queue = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise ImportStopped(f"preflight queue is unreadable: {self.queue_path}") from error
        if queue.get("schema_version") != "docmind-ai-ref-queue-v1":
            raise ImportStopped("preflight queue schema is not supported")
        if queue.get("disposition") != self.queue_disposition:
            raise ImportStopped(
                "queue disposition does not match the explicitly selected mode: "
                f"expected={self.queue_disposition} actual={queue.get('disposition')}"
            )
        records = queue.get("files")
        if not isinstance(records, list) or queue.get("count") != len(records):
            raise ImportStopped("preflight queue count does not match its file list")

        manifest_path = self.queue_path.with_name("ai-ref-preflight-manifest.json")
        if not manifest_path.is_file():
            raise ImportStopped(f"preflight manifest does not exist: {manifest_path}")
        actual_manifest_sha256 = self.file_sha256(manifest_path)
        expected_manifest_sha256 = str(queue.get("source_manifest_sha256") or "")
        if not hmac.compare_digest(actual_manifest_sha256, expected_manifest_sha256):
            raise ImportStopped("preflight queue does not match the manifest")
        self.preflight_manifest_sha256 = actual_manifest_sha256
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise ImportStopped(f"preflight manifest is unreadable: {manifest_path}") from error
        manifest_records = manifest.get("files")
        if not isinstance(manifest_records, list):
            raise ImportStopped("preflight manifest does not contain a file list")
        manifest_by_path = {
            str(record.get("relative_path") or ""): record for record in manifest_records
        }
        if len(manifest_by_path) != len(manifest_records) or "" in manifest_by_path:
            raise ImportStopped("preflight manifest contains duplicate or empty paths")
        self.preflight_paths = set(manifest_by_path)
        expected_queue_paths = {
            relative
            for relative, record in manifest_by_path.items()
            if record.get("disposition") == self.queue_disposition
        }

        queued: list[tuple[Path, str, dict[str, Any]]] = []
        seen: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise ImportStopped("preflight queue contains a non-object entry")
            relative = str(record.get("relative_path") or "")
            relative_path = PurePosixPath(relative)
            if (
                not relative
                or relative_path.is_absolute()
                or ".." in relative_path.parts
                or relative_path.suffix.casefold() != ".pdf"
            ):
                raise ImportStopped(f"preflight queue contains an unsafe PDF path: {relative!r}")
            if relative in seen:
                raise ImportStopped(f"preflight queue contains a duplicate path: {relative}")
            seen.add(relative)
            manifest_record = manifest_by_path.get(relative)
            if (
                not manifest_record
                or manifest_record.get("disposition") != self.queue_disposition
            ):
                raise ImportStopped(
                    "queue entry does not match the selected manifest disposition: "
                    f"{relative}"
                )
            expected_sha256 = str(record.get("sha256") or "")
            if (
                len(expected_sha256) != 64
                or expected_sha256 != str(manifest_record.get("sha256") or "")
            ):
                raise ImportStopped(f"queue SHA-256 does not match the manifest: {relative}")
            if int(record.get("surya_page_count") or 0) > self.max_surya_pages:
                raise ImportStopped(
                    "queue entry exceeds the selected Surya page cap: "
                    f"{relative} pages={record.get('surya_page_count')} "
                    f"cap={self.max_surya_pages}"
                )
            absolute = (self.source / Path(*relative_path.parts)).resolve()
            try:
                absolute.relative_to(self.source)
            except ValueError as error:
                raise ImportStopped(f"queue path escapes the source root: {relative}") from error
            if not absolute.is_file():
                raise ImportStopped(f"queued source file does not exist: {relative}")
            queued.append((absolute, relative, record))
        if seen != expected_queue_paths:
            raise ImportStopped(
                "preflight queue paths do not exactly match the selected manifest disposition"
            )
        self.queue_max_surya_pages = max(
            (int(record.get("surya_page_count") or 0) for _, _, record in queued),
            default=0,
        )
        return queued

    def existing_documents(self) -> dict[str, str]:
        data = self.curl_json("/api/v1/docmind/admin/hierarchy")
        result: dict[str, str] = {}
        for node in data.get("nodes", []):
            if node.get("type") == "folder":
                continue
            relative = str(node.get("relative_path") or "")
            document_id = str(node.get("document_id") or "")
            if relative:
                result[relative] = document_id
        return result

    def registrations(self) -> list[dict[str, Any]]:
        data = self.curl_json("/api/v1/docmind/admin/registrations")
        return list(data.get("registrations") or [])

    def document_status(self, dataset_id: str, document_id: str) -> dict[str, Any]:
        data = self.curl_json(
            f"/api/v1/datasets/{dataset_id}/documents?id={document_id}&page_size=1"
        )
        documents = list(data.get("docs") or [])
        return dict(documents[0]) if documents else {}

    def chunks(self, dataset_id: str, document_id: str) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        page = 1
        total: int | None = None
        while total is None or len(chunks) < total:
            data = self.curl_json(
                f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks"
                f"?page={page}&page_size=100",
                timeout=60,
            )
            batch = list(data.get("chunks") or data.get("items") or [])
            if total is None:
                total = int(data.get("total") or len(batch))
            chunks.extend(batch)
            if not batch:
                break
            page += 1
        return chunks

    @staticmethod
    def chunk_quality(
        chunks: list[dict[str, Any]],
        expected_chunker_version: str,
        accepted_chunker_versions: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any]:
        platforms = [item.get("parser_platform") or {} for item in chunks]
        texts = [(item.get("content") or "").strip() for item in chunks]
        source_keys = []
        for item, text, platform in zip(chunks, texts, platforms):
            source_ids = tuple(sorted(platform.get("source_item_ids") or []))
            source_signature = source_ids or (
                json.dumps(
                    item.get("provenance") or [],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            source_keys.append((text, source_signature))
        result = {
            "quality_gate_version": QUALITY_GATE_VERSION,
            "chunks": len(chunks),
            "short_lt48": sum(
                int(platform.get("chunk_token_count") or 0) < 48
                and not platform.get("short_chunk_exempt")
                for platform in platforms
            ),
            "atomic_short_media": sum(
                int(platform.get("chunk_token_count") or 0) < 48
                and bool(platform.get("short_chunk_exempt"))
                and platform.get("block_type") == "image"
                for platform in platforms
            ),
            "invalid_short_exemption": sum(
                int(platform.get("chunk_token_count") or 0) < 48
                and bool(platform.get("short_chunk_exempt"))
                and platform.get("block_type") != "image"
                for platform in platforms
            ),
            "over512": sum(
                int(platform.get("chunk_token_count") or 0) > 512
                for platform in platforms
            ),
            "invalid_overlap": sum(
                int(platform.get("chunk_overlap_tokens") or 0) < 0
                or int(platform.get("chunk_overlap_tokens") or 0) > 32
                or (
                    int(platform.get("chunk_overlap_tokens") or 0) > 0
                    and (
                        not platform.get("overlap_source_item_ids")
                        or not platform.get("overlap_provenance")
                    )
                )
                for platform in platforms
            ),
            "missing_overlap_metadata": sum(
                platform.get("chunker_version") == EXPECTED_CHUNKER_VERSION
                and "chunk_overlap_tokens" not in platform
                for platform in platforms
            ),
            "missing_bbox": sum(not item.get("positions") for item in chunks),
            "missing_provenance": sum(not item.get("provenance") for item in chunks),
            "duplicates": sum(
                count - 1
                for count in collections.Counter(source_keys).values()
                if count > 1
            ),
            "repeated_content": sum(
                count - 1
                for count in collections.Counter(texts).values()
                if count > 1
            ),
            "chunker_versions": sorted(
                {str(platform.get("chunker_version") or "") for platform in platforms}
            ),
        }
        issues = [
            key
            for key in (
                "short_lt48",
                "invalid_short_exemption",
                "over512",
                "invalid_overlap",
                "missing_overlap_metadata",
                "missing_bbox",
                "missing_provenance",
                "duplicates",
            )
            if result[key]
        ]
        if not chunks:
            issues.append("empty_chunk_set")
        accepted_versions = set(accepted_chunker_versions or {expected_chunker_version})
        if not result["chunker_versions"] or not set(result["chunker_versions"]).issubset(accepted_versions):
            issues.append("unexpected_chunker_version")
        result["issues"] = issues
        return result

    def validate_registration_chunks(
        self, registration: dict[str, Any], relative: str
    ) -> dict[str, Any]:
        dataset_id = str(registration.get("dataset_id") or "")
        document_id = str(registration.get("document_id") or "")
        if not dataset_id or not document_id:
            raise ImportStopped(f"registration lacks dataset/document identity: {relative}")
        quality = self.chunk_quality(
            self.chunks(dataset_id, document_id),
            self.expected_chunker_version,
            {self.expected_chunker_version, *LEGACY_ACCEPTED_CHUNKER_VERSIONS},
        )
        expected_count = int(registration.get("chunk_count") or 0)
        quality["registration_chunk_count"] = expected_count
        quality["chunk_count_consistent"] = quality["chunks"] == expected_count
        if quality["issues"]:
            deferred = dict(registration)
            deferred["error_code"] = "CHUNK_QUALITY_GATE_FAILED"
            deferred["quality_gate"] = quality
            raise RegistrationDeferred(relative, deferred)
        event("chunk_quality_passed", relative_path=relative, **quality)
        return quality

    @staticmethod
    def chunk_set_id(chunks: list[dict[str, Any]]) -> str:
        return str(chunks[0].get("chunk_set_id") or "") if chunks else ""

    def reprocess_stale_registration(
        self, registration: dict[str, Any], relative: str
    ) -> dict[str, Any]:
        dataset_id = str(registration.get("dataset_id") or "")
        document_id = str(registration.get("document_id") or "")
        if not dataset_id or not document_id:
            raise ImportStopped(f"registration lacks dataset/document identity: {relative}")
        old_chunk_set = self.chunk_set_id(self.chunks(dataset_id, document_id))
        failed_retry = (
            str(registration.get("state") or "") in TERMINAL_FAILURE
            and self.registration_is_auto_retryable(registration, relative)
        )
        if not old_chunk_set and not failed_retry:
            raise ImportStopped(f"existing document has no active chunk set: {relative}")
        if failed_retry:
            registration_id = str(registration.get("registration_id") or "")
            if not registration_id:
                raise ImportStopped(f"failed registration lacks identity: {relative}")
            retry_key = "ai-ref-registration-retry-v1-" + hashlib.sha256(
                registration_id.encode("utf-8")
            ).hexdigest()
            retried = self.curl_json(
                f"/api/v1/docmind/admin/registrations/{registration_id}/retry",
                method="POST",
                arguments=["-H", f"Idempotency-Key: {retry_key}"],
                timeout=60,
            )
            target_registration_id = str(retried.get("registration_id") or "")
            if not target_registration_id:
                raise ImportStopped(
                    f"registration retry did not return an identity: {relative}"
                )
        else:
            self.curl_json(
                f"/api/v1/datasets/{dataset_id}/documents/parse",
                method="POST",
                arguments=[
                    "-H",
                    "Content-Type: application/json",
                    "--data-binary",
                    json.dumps(
                        {"document_ids": [document_id]}, separators=(",", ":")
                    ),
                ],
                timeout=60,
            )
            target_registration_id = str(registration.get("registration_id") or "")
        event(
            "stale_document_reprocess_started",
            relative_path=relative,
            old_chunk_set=old_chunk_set,
            expected_chunker_version=self.expected_chunker_version,
            registration_retry=failed_retry,
        )

        started = time.monotonic()
        deadline = started + self.timeout_seconds
        warning_emitted = False
        last_state = None
        last_gate = 0.0
        while time.monotonic() < deadline:
            current = next(
                (
                    item
                    for item in self.registrations()
                    if item.get("registration_id") == target_registration_id
                    or (
                        item.get("document_id") == document_id
                        and item.get("is_current")
                    )
                ),
                None,
            )
            document = self.document_status(dataset_id, document_id)
            state = str(document.get("run") or "UNKNOWN")
            if state != last_state:
                event(
                    "stale_document_reprocess_state",
                    relative_path=relative,
                    state=state,
                    progress=document.get("progress"),
                )
                last_state = state
            if state in {"FAIL", "CANCEL"} and current:
                raise RegistrationDeferred(relative, current)
            if state == "DONE" and current:
                new_chunk_set = self.chunk_set_id(self.chunks(dataset_id, document_id))
                registration_indexed = str(current.get("state") or "") in TERMINAL_SUCCESS
                chunk_set_replaced = bool(
                    new_chunk_set
                    and (not old_chunk_set or new_chunk_set != old_chunk_set)
                )
                if new_chunk_set and (
                    (failed_retry and registration_indexed)
                    or (not failed_retry and chunk_set_replaced)
                ):
                    event(
                        "stale_document_reprocessed",
                        relative_path=relative,
                        elapsed_seconds=round(time.monotonic() - started, 1),
                        old_chunk_set=old_chunk_set,
                        new_chunk_set=new_chunk_set,
                    )
                    return current

            now = time.monotonic()
            if not warning_emitted and now - started >= self.warning_seconds:
                event(
                    "registration_slow",
                    relative_path=relative,
                    elapsed_seconds=round(now - started, 1),
                    hard_timeout_seconds=self.timeout_seconds,
                )
                warning_emitted = True
            if now - last_gate >= 15:
                self.resource_gate()
                last_gate = now
            time.sleep(self.poll_seconds)
        raise ImportStopped(f"stale document reprocessing timed out: {relative}")

    def validate_or_reprocess_stale(
        self, registration: dict[str, Any], relative: str
    ) -> dict[str, Any]:
        try:
            self.validate_registration_chunks(registration, relative)
            return registration
        except RegistrationDeferred as deferred:
            quality_gate = deferred.registration.get("quality_gate") or {}
            issues = set(quality_gate.get("issues") or [])
            chunker_versions = set(quality_gate.get("chunker_versions") or [])
            legacy_short_repair = (
                issues == {"short_lt48"}
                and self.expected_chunker_version not in chunker_versions
            )
            if (
                "unexpected_chunker_version" not in issues
                and not legacy_short_repair
            ):
                raise
        try:
            refreshed = self.reprocess_stale_registration(registration, relative)
        except RegistrationDeferred as deferred:
            refreshed = self.recover_retryable_registration(
                deferred.registration, relative
            )
        self.validate_registration_chunks(refreshed, relative)
        return refreshed

    def catalog_version(self) -> str | None:
        data = self.curl_json("/api/v1/docmind/folders")
        value = str(data.get("catalog_version_id") or "").strip()
        return value or None

    def docker_states(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for container in DEFAULT_CONTAINERS:
            command = ["docker", "inspect", container]
            completed = subprocess.run(command, capture_output=True, text=True)
            if completed.returncode:
                raise ImportStopped(f"required container missing: {container}")
            inspected = json.loads(completed.stdout)[0]
            state = dict(inspected["State"])
            state["RestartCount"] = int(inspected.get("RestartCount") or 0)
            result[container] = state
        return result

    @staticmethod
    def configured_surya_page_cap() -> int:
        completed = subprocess.run(
            ["docker", "inspect", "docmind-ragflow-cpu-1"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode:
            raise ImportStopped("could not inspect the DocMind application container")
        inspected = json.loads(completed.stdout)[0]
        prefix = "PARSER_PLATFORM_PDF_ROUTING_SURYA_FALLBACK_MAX_PAGES="
        configured = next(
            (
                value.removeprefix(prefix)
                for value in inspected.get("Config", {}).get("Env", [])
                if value.startswith(prefix)
            ),
            "20",
        )
        try:
            cap = int(configured)
        except ValueError as error:
            raise ImportStopped("runtime Surya page cap is not an integer") from error
        if cap <= 0:
            raise ImportStopped("runtime Surya page cap must be positive")
        return cap

    def validate_runtime_surya_page_cap(self) -> None:
        self.runtime_surya_page_cap = self.configured_surya_page_cap()
        if self.runtime_surya_page_cap < self.queue_max_surya_pages:
            raise ImportStopped(
                "runtime Surya page cap is lower than the selected queue requires: "
                f"runtime={self.runtime_surya_page_cap} "
                f"queue_max={self.queue_max_surya_pages}"
            )

    def establish_resource_baseline(self) -> None:
        states = self.docker_states()
        self.baseline_restarts = {name: int(state.get("RestartCount") or 0) for name, state in states.items()}
        self.baseline_surya_watchdog_events = self.surya_watchdog_event_count()
        self.baseline_swap_used_kib = self.swap_used_kib()
        self.resource_gate(states)

    @staticmethod
    def surya_watchdog_event_count() -> int:
        completed = subprocess.run(
            ["docker", "logs", SURYA_CONTAINER],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode:
            raise ImportStopped("could not read Surya watchdog events")
        return (completed.stdout + completed.stderr).count("surya_timeout scope=")

    def recover_surya_watchdog_restart(
        self, state: dict[str, Any]
    ) -> dict[str, Any]:
        restart_count = int(state.get("RestartCount") or 0)
        baseline = self.baseline_restarts.get(SURYA_CONTAINER, 0)
        if restart_count <= baseline:
            return state

        watchdog_events = self.surya_watchdog_event_count()
        if watchdog_events <= self.baseline_surya_watchdog_events:
            raise ImportStopped(
                f"container restart count increased: {SURYA_CONTAINER}"
            )
        event(
            "surya_watchdog_restart_detected",
            restart_count=restart_count,
            watchdog_events=watchdog_events,
        )

        deadline = time.monotonic() + 180
        latest = state
        while time.monotonic() < deadline:
            if bool(latest.get("OOMKilled")):
                raise ImportStopped(f"container OOMKilled: {SURYA_CONTAINER}")
            health = (latest.get("Health") or {}).get("Status")
            if latest.get("Status") == "running" and health == "healthy":
                self.baseline_restarts[SURYA_CONTAINER] = int(
                    latest.get("RestartCount") or restart_count
                )
                self.baseline_surya_watchdog_events = watchdog_events
                self.baseline_swap_used_kib = self.swap_used_kib()
                self.swap_high_samples = 0
                event(
                    "surya_watchdog_recovered",
                    restart_count=self.baseline_restarts[SURYA_CONTAINER],
                    watchdog_events=watchdog_events,
                )
                return latest
            time.sleep(5)
            latest = self.docker_states()[SURYA_CONTAINER]
        raise ImportStopped("Surya watchdog restart health timeout")

    @staticmethod
    def swap_used_kib() -> int:
        meminfo: dict[str, int] = {}
        with Path("/proc/meminfo").open(encoding="utf-8") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                meminfo[key] = int(value.strip().split()[0])
        return meminfo["SwapTotal"] - meminfo["SwapFree"]

    @staticmethod
    def swap_growth_kib(swap_used_kib: int, baseline_swap_used_kib: int) -> int:
        return max(0, swap_used_kib - baseline_swap_used_kib)

    def container_memory_percent(self, container: str) -> float:
        completed = subprocess.run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.MemPerc}}",
                container,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode:
            raise ImportStopped(f"could not read container memory: {container}")
        try:
            return float(completed.stdout.strip().removesuffix("%"))
        except ValueError as error:
            raise ImportStopped(f"invalid container memory value: {container}") from error

    def recycle_surya_if_needed(self) -> None:
        threshold = self.surya_recycle_memory_percent
        if threshold <= 0:
            return
        memory_percent = self.container_memory_percent(SURYA_CONTAINER)
        if memory_percent < threshold:
            return
        event(
            "surya_recycle_started",
            memory_percent=round(memory_percent, 2),
            threshold_percent=threshold,
        )
        completed = subprocess.run(
            ["docker", "restart", SURYA_CONTAINER],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode:
            raise ImportStopped("controlled Surya recycle failed")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = self.docker_states()[SURYA_CONTAINER]
            health = (state.get("Health") or {}).get("Status")
            if state.get("Status") == "running" and health == "healthy":
                self.baseline_restarts[SURYA_CONTAINER] = int(
                    state.get("RestartCount") or 0
                )
                self.baseline_swap_used_kib = self.swap_used_kib()
                self.swap_high_samples = 0
                event(
                    "surya_recycle_completed",
                    memory_percent=round(
                        self.container_memory_percent(SURYA_CONTAINER), 2
                    ),
                    swap_baseline_kib=self.baseline_swap_used_kib,
                )
                return
            time.sleep(5)
        raise ImportStopped("controlled Surya recycle health timeout")

    def resource_gate(self, states: dict[str, dict[str, Any]] | None = None) -> None:
        states = states or self.docker_states()
        states[SURYA_CONTAINER] = self.recover_surya_watchdog_restart(
            states[SURYA_CONTAINER]
        )
        for name, state in states.items():
            if state.get("Status") != "running":
                raise ImportStopped(f"container is not running: {name}")
            if bool(state.get("OOMKilled")):
                raise ImportStopped(f"container OOMKilled: {name}")
            if int(state.get("RestartCount") or 0) > self.baseline_restarts.get(name, 0):
                raise ImportStopped(f"container restart count increased: {name}")
            health = (state.get("Health") or {}).get("Status")
            if health and health != "healthy":
                raise ImportStopped(f"container is not healthy: {name} ({health})")

        swap_used = self.swap_used_kib()
        swap_growth = self.swap_growth_kib(
            swap_used, self.baseline_swap_used_kib
        )
        if swap_growth > self.swap_limit_kib:
            self.swap_high_samples += 1
        else:
            self.swap_high_samples = 0
        if self.swap_high_samples >= 3:
            raise ImportStopped(
                "swap usage increased beyond the limit for three consecutive checks"
            )

        current_catalog = self.catalog_version()
        if current_catalog != self.catalog_version_id:
            raise ImportStopped("active Catalog changed during import")

    def wait_for_registration(self, registration_id: str, document_id: str, relative: str) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        last_state = None
        last_gate = 0.0
        pending_since: float | None = None
        pending_requeue_attempted = False
        warning_emitted = False
        while time.monotonic() < deadline:
            now = time.monotonic()
            registrations = self.registrations()
            registration = next(
                (
                    item
                    for item in registrations
                    if item.get("registration_id") == registration_id
                    or (document_id and item.get("document_id") == document_id and item.get("is_current"))
                ),
                None,
            )
            if registration:
                state = str(registration.get("state") or "")
                if state != last_state:
                    event(
                        "registration_state",
                        relative_path=relative,
                        state=state,
                        progress=registration.get("progress"),
                        chunks=registration.get("chunk_count"),
                    )
                    last_state = state
                if state in TERMINAL_SUCCESS:
                    return registration
                if state in TERMINAL_FAILURE:
                    raise RegistrationDeferred(relative, registration)
                dataset_id = str(registration.get("dataset_id") or "")
                document = (
                    self.document_status(dataset_id, document_id)
                    if dataset_id and document_id
                    else {}
                )
                run = str(document.get("run") or "")
                if state in {"INDEX_QUEUED", "INDEXING"} and run == "PENDING":
                    if pending_since is None:
                        pending_since = now
                    elif (
                        not pending_requeue_attempted
                        and now - pending_since
                        >= PENDING_REGISTRATION_REQUEUE_SECONDS
                    ):
                        self.curl_json(
                            f"/api/v1/datasets/{dataset_id}/documents/parse",
                            method="POST",
                            arguments=[
                                "-H",
                                "Content-Type: application/json",
                                "--data-binary",
                                json.dumps(
                                    {"document_ids": [document_id]},
                                    separators=(",", ":"),
                                ),
                            ],
                            timeout=60,
                        )
                        event(
                            "pending_registration_requeued",
                            relative_path=relative,
                            elapsed_seconds=round(now - pending_since, 1),
                        )
                        pending_requeue_attempted = True
                else:
                    pending_since = None

            if not warning_emitted and now - started >= self.warning_seconds:
                event(
                    "registration_slow",
                    relative_path=relative,
                    elapsed_seconds=round(now - started, 1),
                    hard_timeout_seconds=self.timeout_seconds,
                )
                warning_emitted = True
            if now - last_gate >= 15:
                self.resource_gate()
                last_gate = now
            time.sleep(self.poll_seconds)
        raise ImportStopped(f"registration timed out: {relative}")

    def wait_if_current(
        self, document_id: str, relative: str
    ) -> dict[str, Any] | None:
        if not document_id:
            return None
        registration = next(
            (item for item in self.registrations() if item.get("document_id") == document_id and item.get("is_current")),
            None,
        )
        if not registration:
            return None
        state = str(registration.get("state") or "")
        if state in TERMINAL_SUCCESS:
            return registration
        if state in TERMINAL_FAILURE:
            if self.registration_is_auto_retryable(registration, relative):
                return self.recover_retryable_registration(registration, relative)
            raise RegistrationDeferred(relative, registration)
        return self.wait_for_registration(
            str(registration["registration_id"]), document_id, relative
        )

    def import_one(
        self, absolute: Path, relative: str, expected_sha256: str
    ) -> dict[str, Any]:
        actual_sha256 = self.file_sha256(absolute)
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise ImportStopped(f"source PDF changed after preflight: {relative}")
        idempotency_key = "ai-ref-sequential-v1-" + hashlib.sha256(
            (relative + "\0" + actual_sha256).encode("utf-8")
        ).hexdigest()
        self.refresh_session()
        data = self.curl_json(
            "/api/v1/docmind/admin/hierarchy/imports",
            method="POST",
            timeout=900,
            arguments=[
                "-H",
                f"Idempotency-Key: {idempotency_key}",
                "--form-string",
                f"path={relative}",
                "--form-escape",
                "-F",
                self.curl_file_form(absolute),
            ],
        )
        if data.get("state") != "COMPLETED" or int(data.get("failed_count") or 0):
            items = list(data.get("items") or [])
            item = items[0] if len(items) == 1 and isinstance(items[0], dict) else {}
            if str(item.get("error_code") or "") == "PARSER_SOURCE_TYPE_MISMATCH":
                raise RegistrationDeferred(relative, item)
            raise ImportStopped(f"import job failed: {relative} state={data.get('state')}")
        items = list(data.get("items") or [])
        if len(items) != 1 or items[0].get("state") != "SUCCEEDED":
            raise ImportStopped(f"import item failed: {relative}")
        item = items[0]
        try:
            return self.wait_for_registration(
                str(item.get("registration_id") or ""),
                str(item.get("document_id") or ""),
                relative,
            )
        except RegistrationDeferred as deferred:
            return self.recover_retryable_registration(deferred.registration, relative)

    def run(self, *, dry_run: bool, limit: int | None) -> int:
        if not self.source.is_dir():
            raise ImportStopped(f"source directory does not exist: {self.source}")
        self.load_runtime_review_queue()
        all_pdfs = self.pdf_paths()
        queued_pdfs = self.queued_pdf_paths()
        self.total = len(all_pdfs)
        current_paths = {relative for _, relative in all_pdfs}
        if current_paths != self.preflight_paths:
            raise ImportStopped("source PDF inventory changed after preflight")
        self.eligible = len(queued_pdfs)
        if self.queue_disposition == "ready_auto":
            self.deferred_review = self.total - self.eligible
            self.out_of_scope = 0
        else:
            self.deferred_review = 0
            self.out_of_scope = self.total - self.eligible
        selected = queued_pdfs[:limit] if limit else queued_pdfs
        event(
            "inventory",
            total_pdf=self.total,
            eligible_pdf=self.eligible,
            queue_disposition=self.queue_disposition,
            max_surya_pages=self.max_surya_pages,
            deferred_review=self.deferred_review,
            out_of_scope=self.out_of_scope,
            selected=len(selected),
            source=str(self.source),
            queue=str(self.queue_path),
            preflight_manifest_sha256=self.preflight_manifest_sha256,
        )
        if dry_run:
            for _, relative, record in selected:
                event(
                    "dry_run_candidate",
                    relative_path=relative,
                    route=record.get("route"),
                    page_count=record.get("page_count"),
                    surya_page_count=record.get("surya_page_count"),
                )
            self.write_state("DRY_RUN", selected=len(selected))
            return 0

        self.refresh_session()
        self.catalog_version_id = self.catalog_version()
        existing = self.existing_documents()
        self.establish_resource_baseline()
        self.validate_runtime_surya_page_cap()
        self.write_state("RUNNING")
        event(
            "started",
            total_pdf=self.total,
            selected=len(selected),
            existing=len(existing),
            catalog_version_id=self.catalog_version_id,
            runtime_surya_page_cap=self.runtime_surya_page_cap,
        )

        for absolute, relative, record in selected:
            self.current = relative
            self.write_state("RUNNING")
            if relative in self.runtime_deferred_records:
                event(
                    "skipped_runtime_deferred",
                    relative_path=relative,
                    error_code=self.runtime_deferred_records[relative].get("error_code"),
                )
                continue
            if relative in existing:
                try:
                    current_registration = self.wait_if_current(
                        existing[relative], relative
                    )
                    if current_registration:
                        self.validate_or_reprocess_stale(
                            current_registration, relative
                        )
                    self.consecutive_deferred = 0
                except RegistrationDeferred as deferred:
                    self.defer_registration(relative, record, deferred.registration)
                    self.recycle_surya_if_needed()
                    self.resource_gate()
                    continue
                self.skipped += 1
                event("skipped_existing", relative_path=relative)
                continue
            if not absolute.is_file():
                raise ImportStopped(f"source file disappeared: {relative}")

            started = time.monotonic()
            event("import_started", relative_path=relative, size_bytes=absolute.stat().st_size)
            try:
                registration = self.import_one(
                    absolute, relative, str(record["sha256"])
                )
                self.validate_registration_chunks(registration, relative)
            except RegistrationDeferred as deferred:
                self.defer_registration(relative, record, deferred.registration)
                self.recycle_surya_if_needed()
                self.resource_gate()
                continue
            self.completed += 1
            self.consecutive_deferred = 0
            existing[relative] = str(registration.get("document_id") or "")
            event(
                "import_indexed",
                relative_path=relative,
                elapsed_seconds=round(time.monotonic() - started, 1),
                chunks=registration.get("chunk_count"),
                parser=(registration.get("parser_run") or {}).get("parser_name"),
                backend=(registration.get("parser_run") or {}).get("backend"),
            )
            self.recycle_surya_if_needed()
            self.resource_gate()

        self.current = None
        self.write_state("COMPLETED")
        event(
            "completed",
            completed=self.completed,
            skipped_existing=self.skipped,
            deferred_runtime=self.deferred_runtime,
            total_pdf=self.total,
        )
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("/home/uplexsoft/sample_doc/ai-ref"))
    parser.add_argument(
        "--queue",
        type=Path,
        default=Path(
            "/home/uplexsoft/docmind-data/capability-artifacts/"
            "ai-ref-preflight/ai-ref-ready-auto.json"
        ),
    )
    parser.add_argument(
        "--runtime-review-queue",
        type=Path,
        default=Path(
            "/home/uplexsoft/docmind-data/capability-artifacts/"
            "ai-ref-import/ai-ref-runtime-review-queue.json"
        ),
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:9380")
    parser.add_argument("--cookie", type=Path, default=Path("/home/uplexsoft/docmind/runtime/private/docmind-shared-session.cookie"))
    parser.add_argument("--state", type=Path, default=Path("/home/uplexsoft/docmind-data/capability-artifacts/ai-ref-sequential-import-state.json"))
    parser.add_argument("--lock", type=Path, default=Path("/home/uplexsoft/docmind-data/capability-artifacts/ai-ref-sequential-import.lock"))
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--warning-seconds", type=int, default=900)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--swap-limit-gib", type=int, default=2)
    parser.add_argument(
        "--expected-chunker-version", default=EXPECTED_CHUNKER_VERSION
    )
    parser.add_argument("--max-consecutive-deferred", type=int, default=3)
    parser.add_argument(
        "--queue-disposition",
        choices=("ready_auto", "review_required"),
        default="ready_auto",
    )
    parser.add_argument("--max-surya-pages", type=int, default=20)
    parser.add_argument("--max-parser-retries", type=int, default=2)
    parser.add_argument("--surya-recycle-memory-percent", type=float, default=85.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_surya_pages <= 0:
        event("stopped", reason="--max-surya-pages must be positive")
        return 2
    if args.max_parser_retries < 0:
        event("stopped", reason="--max-parser-retries must not be negative")
        return 2
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = args.lock.open("a+", encoding="utf-8")
    os.chmod(args.lock, 0o600)
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        event("stopped", reason="another sequential importer is already running")
        return 2

    importer = SequentialImporter(args)
    try:
        return importer.run(dry_run=args.dry_run, limit=args.limit)
    except (ImportStopped, subprocess.TimeoutExpired, OSError) as error:
        importer.failed += 1
        importer.write_state("STOPPED", reason=str(error))
        event("stopped", reason=str(error), current_relative_path=importer.current)
        return 1


if __name__ == "__main__":
    sys.exit(main())
