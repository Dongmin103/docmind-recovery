from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = ROOT / "scripts" / "docmind_ai_ref_sequential_import.py"
SPEC = importlib.util.spec_from_file_location("docmind_ai_ref_sequential_import", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _chunk(
    content: str,
    tokens: int,
    *,
    block_type: str = "text",
    exempt: bool = False,
    page: int = 1,
    source_ids: list[str] | None = None,
    version: str = "2.93.6",
    overlap_tokens: int | None = None,
) -> dict:
    return {
        "content": content,
        "positions": [[page, 10, 20, 30, 40]],
        "provenance": [{"page": page, "bbox": [10, 20, 30, 40]}],
        "parser_platform": {
            "block_type": block_type,
            "chunk_token_count": tokens,
            "chunker_version": version,
            "short_chunk_exempt": exempt,
            "source_item_ids": source_ids or [f"pdf:p{page}:b1"],
            **(
                {
                    "chunk_overlap_tokens": overlap_tokens,
                    "overlap_source_item_ids": [f"pdf:p{page}:b0"] if overlap_tokens else [],
                    "overlap_provenance": (
                        [{"page": page, "bbox": [10, 20, 30, 40]}]
                        if overlap_tokens
                        else []
                    ),
                }
                if overlap_tokens is not None
                else {}
            ),
        },
    }


def test_chunk_quality_allows_only_atomic_short_images() -> None:
    quality = MODULE.SequentialImporter.chunk_quality(
        [
            _chunk("complete paragraph", 64),
            _chunk("informative figure", 20, block_type="image", exempt=True),
        ],
        "2.93.6",
    )

    assert quality["issues"] == []
    assert quality["short_lt48"] == 0
    assert quality["atomic_short_media"] == 1


def test_chunk_quality_rejects_short_text_and_invalid_exemption() -> None:
    quality = MODULE.SequentialImporter.chunk_quality(
        [
            _chunk("short text", 12),
            _chunk("hidden short text", 20, exempt=True),
        ],
        "2.93.6",
    )

    assert quality["short_lt48"] == 1
    assert quality["invalid_short_exemption"] == 1
    assert set(quality["issues"]) == {"short_lt48", "invalid_short_exemption"}


def test_registration_quality_failure_is_deferred() -> None:
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    importer.expected_chunker_version = "2.93.6"
    importer.chunks = lambda dataset_id, document_id: [_chunk("short text", 12)]
    registration = {
        "dataset_id": "dataset-id",
        "document_id": "document-id",
        "registration_id": "registration-id",
        "chunk_count": 1,
        "state": "INDEXED",
    }

    try:
        MODULE.SequentialImporter.validate_registration_chunks(
            importer, registration, "folder/file.pdf"
        )
    except MODULE.RegistrationDeferred as deferred:
        assert deferred.registration["error_code"] == "CHUNK_QUALITY_GATE_FAILED"
        assert deferred.registration["quality_gate"]["issues"] == ["short_lt48"]
    else:
        raise AssertionError("quality failure was not deferred")


def test_repeated_content_on_distinct_pages_is_informational() -> None:
    quality = MODULE.SequentialImporter.chunk_quality(
        [
            _chunk("Repeated source slide", 64, page=10),
            _chunk("Repeated source slide", 64, page=11),
        ],
        "2.93.6",
    )

    assert quality["duplicates"] == 0
    assert quality["repeated_content"] == 1
    assert quality["issues"] == []


def test_repeated_content_from_the_same_source_is_rejected() -> None:
    quality = MODULE.SequentialImporter.chunk_quality(
        [
            _chunk("Duplicated parser output", 64, source_ids=["pdf:p1:b1"]),
            _chunk("Duplicated parser output", 64, source_ids=["pdf:p1:b1"]),
        ],
        "2.93.6",
    )

    assert quality["duplicates"] == 1
    assert quality["issues"] == ["duplicates"]


def test_chunk_quality_accepts_legacy_and_valid_new_overlap() -> None:
    quality = MODULE.SequentialImporter.chunk_quality(
        [
            _chunk("legacy", 64, version="2.93.6"),
            _chunk("new", 96, version="2.94.2", overlap_tokens=24, page=2),
        ],
        "2.94.2",
        {"2.93.6", "2.94.2"},
    )

    assert quality["issues"] == []
    assert quality["chunker_versions"] == ["2.93.6", "2.94.2"]


def test_chunk_quality_rejects_invalid_overlap_metadata() -> None:
    quality = MODULE.SequentialImporter.chunk_quality(
        [_chunk("invalid", 96, version="2.94.2", overlap_tokens=33)],
        "2.94.2",
    )

    assert quality["issues"] == ["invalid_overlap"]


def test_document_parser_failure_is_deferred() -> None:
    importer = SimpleNamespace(
        timeout_seconds=10,
        warning_seconds=2,
        poll_seconds=1,
        registrations=lambda: [
            {
                "registration_id": "registration-id",
                "document_id": "document-id",
                "is_current": True,
                "state": "FAILED",
                "error_code": "PARSER_DOCUMENT_INVALID",
            }
        ],
    )

    try:
        MODULE.SequentialImporter.wait_for_registration(
            importer, "registration-id", "document-id", "folder/file.pdf"
        )
    except MODULE.RegistrationDeferred as deferred:
        assert deferred.registration["error_code"] == "PARSER_DOCUMENT_INVALID"
    else:
        raise AssertionError("parser failure was not deferred")


def test_three_consecutive_deferrals_trip_the_safety_limit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
        importer.runtime_deferred_records = {}
        importer.runtime_review_path = Path(directory) / "review.json"
        importer.queue_path = Path(directory) / "queue.json"
        importer.preflight_manifest_sha256 = "a" * 64
        importer.deferred_runtime = 0
        importer.consecutive_deferred = 2
        importer.max_consecutive_deferred = 3
        importer.write_state = lambda status: None

        try:
            importer.defer_registration(
                "folder/file.pdf",
                {"sha256": "b" * 64, "route": "docling", "page_count": 1},
                {"error_code": "PARSER_DOCUMENT_INVALID"},
            )
        except MODULE.ImportStopped as stopped:
            assert "safety limit" in str(stopped)
            assert importer.runtime_review_path.is_file()
        else:
            raise AssertionError("consecutive deferrals did not stop the importer")


def test_stale_chunker_version_reprocesses_once() -> None:
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    old_registration = {"document_id": "document-id", "version": "old"}
    new_registration = {"document_id": "document-id", "version": "new"}
    calls: list[str] = []

    def validate(registration: dict, relative: str) -> dict:
        calls.append(f"validate:{registration['version']}")
        if registration["version"] == "old":
            deferred = dict(registration)
            deferred["quality_gate"] = {"issues": ["unexpected_chunker_version"]}
            raise MODULE.RegistrationDeferred(relative, deferred)
        return {"issues": []}

    def reprocess(registration: dict, relative: str) -> dict:
        calls.append(f"reprocess:{registration['version']}")
        return new_registration

    importer.validate_registration_chunks = validate
    importer.reprocess_stale_registration = reprocess

    result = importer.validate_or_reprocess_stale(old_registration, "folder/file.pdf")

    assert result is new_registration
    assert calls == ["validate:old", "reprocess:old", "validate:new"]


def test_legacy_short_chunk_reprocesses_after_policy_upgrade() -> None:
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    importer.expected_chunker_version = "2.95.0"
    old_registration = {"document_id": "document-id", "version": "old"}
    new_registration = {"document_id": "document-id", "version": "new"}
    calls: list[str] = []

    def validate(registration: dict, relative: str) -> dict:
        calls.append(f"validate:{registration['version']}")
        if registration["version"] == "old":
            deferred = dict(registration)
            deferred["quality_gate"] = {
                "issues": ["short_lt48"],
                "chunker_versions": ["2.94.2"],
            }
            raise MODULE.RegistrationDeferred(relative, deferred)
        return {"issues": []}

    def reprocess(registration: dict, relative: str) -> dict:
        calls.append(f"reprocess:{registration['version']}")
        return new_registration

    importer.validate_registration_chunks = validate
    importer.reprocess_stale_registration = reprocess

    result = importer.validate_or_reprocess_stale(old_registration, "folder/file.pdf")

    assert result is new_registration
    assert calls == ["validate:old", "reprocess:old", "validate:new"]


def test_stale_short_quality_queue_is_released_once_per_policy() -> None:
    with tempfile.TemporaryDirectory() as directory:
        runtime_path = Path(directory) / "runtime-review.json"
        MODULE.atomic_json(
            runtime_path,
            {
                "schema_version": "docmind-ai-ref-runtime-review-v1",
                "source_manifest_sha256": "a" * 64,
                "count": 1,
                "files": [
                    {
                        "relative_path": "folder/file.pdf",
                        "error_code": "CHUNK_QUALITY_GATE_FAILED",
                        "quality_gate": {
                            "quality_gate_version": MODULE.QUALITY_GATE_VERSION,
                            "issues": ["short_lt48"],
                        },
                    }
                ],
            },
        )
        importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
        importer.runtime_review_path = runtime_path
        importer.queue_path = Path(directory) / "ready.json"

        importer.load_runtime_review_queue()

        assert importer.runtime_deferred_records == {}
        assert importer.deferred_runtime == 0
        persisted = MODULE.json.loads(runtime_path.read_text(encoding="utf-8"))
        assert persisted["count"] == 0


def test_generic_index_failure_is_retryable_once_per_policy() -> None:
    assert "DOCMIND_INDEX_FAILED" in MODULE.AUTO_RETRYABLE_PARSER_ERRORS


def test_surya_page_limit_failure_is_reprocessed_once() -> None:
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    failed_registration = {
        "document_id": "document-id",
        "is_current": True,
        "state": "FAILED",
        "error_code": "PARSER_SURYA_PAGE_LIMIT_EXCEEDED",
    }
    refreshed_registration = {
        "document_id": "document-id",
        "is_current": True,
        "state": "INDEXED",
    }
    calls: list[str] = []
    importer.max_parser_retries = 2
    importer.registrations = lambda: [failed_registration]

    def reprocess(registration: dict, relative: str) -> dict:
        calls.append(relative)
        assert registration is failed_registration
        return refreshed_registration

    importer.reprocess_stale_registration = reprocess
    result = importer.wait_if_current("document-id", "folder/file.pdf")

    assert result is refreshed_registration
    assert calls == ["folder/file.pdf"]


def test_reprocess_tracks_ragflow_document_run_instead_of_stale_registration() -> None:
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    failed_registration = {
        "dataset_id": "dataset-id",
        "document_id": "document-id",
        "registration_id": "registration-id",
        "is_current": True,
        "state": "FAILED",
        "error_code": "PARSER_SURYA_PAGE_LIMIT_EXCEEDED",
    }
    document_states = iter(
        [
            {"run": "RUNNING", "progress": 0.1},
            {"run": "DONE", "progress": 1.0},
        ]
    )
    retrying_registration = {
        "dataset_id": "dataset-id",
        "document_id": "document-id",
        "registration_id": "retry-registration-id",
        "is_current": True,
        "state": "INDEXING",
    }
    indexed_registration = {
        **retrying_registration,
        "state": "INDEXED",
        "chunk_count": 1,
    }
    registrations = iter(
        [
            [retrying_registration],
            [indexed_registration],
        ]
    )
    chunk_sets = iter(
        [
            [],
            [{"chunk_set_id": "new-chunk-set"}],
        ]
    )
    parse_calls: list[str] = []
    importer.expected_chunker_version = "2.93.6"
    importer.timeout_seconds = 10
    importer.warning_seconds = 20
    importer.poll_seconds = 0
    importer.chunks = lambda dataset_id, document_id: next(chunk_sets)
    importer.document_status = lambda dataset_id, document_id: next(document_states)
    importer.registrations = lambda: next(registrations)
    importer.resource_gate = lambda: None

    def curl_json(endpoint: str, **kwargs) -> dict:
        parse_calls.append(endpoint)
        assert kwargs["method"] == "POST"
        assert kwargs["arguments"][0:1] == ["-H"]
        assert kwargs["arguments"][1].startswith("Idempotency-Key: ")
        return {"registration_id": "retry-registration-id"}

    importer.curl_json = curl_json
    result = importer.reprocess_stale_registration(failed_registration, "file.pdf")

    assert result is indexed_registration
    assert parse_calls == [
        "/api/v1/docmind/admin/registrations/registration-id/retry"
    ]


def test_swap_growth_ignores_swap_that_predates_the_batch() -> None:
    gib = 1024 * 1024

    assert MODULE.SequentialImporter.swap_growth_kib(3 * gib, 3 * gib) == 0
    assert MODULE.SequentialImporter.swap_growth_kib(4 * gib, 3 * gib) == gib
    assert MODULE.SequentialImporter.swap_growth_kib(2 * gib, 3 * gib) == 0


def _preflight_queue_importer(
    directory: str,
    *,
    disposition: str,
    selected_disposition: str,
    surya_page_count: int,
    max_surya_pages: int,
) -> MODULE.SequentialImporter:
    root = Path(directory)
    source = root / "source"
    source.mkdir()
    relative = "folder/file.pdf"
    absolute = source / "folder" / "file.pdf"
    absolute.parent.mkdir()
    absolute.write_bytes(b"%PDF-review-queue")
    source_sha256 = MODULE.SequentialImporter.file_sha256(absolute)
    record = {
        "relative_path": relative,
        "sha256": source_sha256,
        "disposition": disposition,
        "route": "hybrid",
        "page_count": surya_page_count + 20,
        "native_page_count": 20,
        "surya_page_count": surya_page_count,
        "surya_page_numbers": list(range(1, surya_page_count + 1)),
    }
    manifest_path = root / "ai-ref-preflight-manifest.json"
    MODULE.atomic_json(
        manifest_path,
        {"schema_version": "docmind-ai-ref-preflight-v1", "files": [record]},
    )
    queue_path = root / "queue.json"
    MODULE.atomic_json(
        queue_path,
        {
            "schema_version": "docmind-ai-ref-queue-v1",
            "disposition": disposition,
            "count": 1,
            "source_manifest_sha256": MODULE.SequentialImporter.file_sha256(
                manifest_path
            ),
            "files": [record],
        },
    )
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    importer.source = source.resolve()
    importer.queue_path = queue_path.resolve()
    importer.queue_disposition = selected_disposition
    importer.max_surya_pages = max_surya_pages
    importer.preflight_manifest_sha256 = None
    importer.preflight_paths = set()
    return importer


def test_review_queue_requires_explicit_mode_and_accepts_bounded_surya_pages() -> None:
    with tempfile.TemporaryDirectory() as directory:
        importer = _preflight_queue_importer(
            directory,
            disposition="review_required",
            selected_disposition="review_required",
            surya_page_count=163,
            max_surya_pages=200,
        )

        queued = importer.queued_pdf_paths()

        assert len(queued) == 1
        assert queued[0][1] == "folder/file.pdf"


def test_review_queue_is_rejected_without_explicit_mode() -> None:
    with tempfile.TemporaryDirectory() as directory:
        importer = _preflight_queue_importer(
            directory,
            disposition="review_required",
            selected_disposition="ready_auto",
            surya_page_count=21,
            max_surya_pages=20,
        )

        try:
            importer.queued_pdf_paths()
        except MODULE.ImportStopped as stopped:
            assert "explicitly selected mode" in str(stopped)
        else:
            raise AssertionError("review queue was accepted without explicit mode")


def test_review_queue_rejects_documents_above_selected_surya_cap() -> None:
    with tempfile.TemporaryDirectory() as directory:
        importer = _preflight_queue_importer(
            directory,
            disposition="review_required",
            selected_disposition="review_required",
            surya_page_count=201,
            max_surya_pages=200,
        )

        try:
            importer.queued_pdf_paths()
        except MODULE.ImportStopped as stopped:
            assert "selected Surya page cap" in str(stopped)
        else:
            raise AssertionError("document above the selected cap was accepted")


def test_runtime_surya_cap_must_cover_the_selected_queue() -> None:
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    importer.queue_max_surya_pages = 163
    importer.runtime_surya_page_cap = None
    importer.configured_surya_page_cap = lambda: 20

    try:
        importer.validate_runtime_surya_page_cap()
    except MODULE.ImportStopped as stopped:
        assert "lower than the selected queue" in str(stopped)
    else:
        raise AssertionError("runtime cap mismatch was accepted")


def test_runtime_surya_cap_accepts_the_selected_queue_maximum() -> None:
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    importer.queue_max_surya_pages = 163
    importer.runtime_surya_page_cap = None
    importer.configured_surya_page_cap = lambda: 200

    importer.validate_runtime_surya_page_cap()

    assert importer.runtime_surya_page_cap == 200


def test_retryable_parser_failure_retries_twice_then_succeeds() -> None:
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    first = {
        "registration_id": "first",
        "state": "FAILED",
        "error_code": "SURYA_PDF_DEADLINE_EXCEEDED",
        "retry_of_id": None,
    }
    second = {
        "registration_id": "second",
        "state": "FAILED",
        "error_code": "PARSER_SURYA_TIMEOUT",
        "retry_of_id": "first",
    }
    third = {
        "registration_id": "third",
        "state": "INDEXED",
        "retry_of_id": "second",
    }
    attempts: list[str] = []
    importer.max_parser_retries = 2
    importer.registrations = lambda: [first, second, third]
    importer.recycle_surya_if_needed = lambda: None
    importer.resource_gate = lambda: None

    def reprocess(registration: dict, relative: str) -> dict:
        attempts.append(str(registration["registration_id"]))
        if registration is first:
            raise MODULE.RegistrationDeferred(relative, second)
        return third

    importer.reprocess_stale_registration = reprocess

    result = importer.recover_retryable_registration(first, "folder/file.pdf")

    assert result is third
    assert attempts == ["first", "second"]


def test_retryable_parser_failure_respects_existing_retry_depth() -> None:
    importer = MODULE.SequentialImporter.__new__(MODULE.SequentialImporter)
    first = {
        "registration_id": "first",
        "state": "FAILED",
        "error_code": "SURYA_PDF_DEADLINE_EXCEEDED",
        "retry_of_id": None,
    }
    second = {
        "registration_id": "second",
        "state": "FAILED",
        "error_code": "SURYA_PDF_DEADLINE_EXCEEDED",
        "retry_of_id": "first",
    }
    importer.max_parser_retries = 1
    importer.registrations = lambda: [first, second]
    importer.reprocess_stale_registration = lambda *_: (_ for _ in ()).throw(
        AssertionError("retry limit was not enforced")
    )

    try:
        importer.recover_retryable_registration(second, "folder/file.pdf")
    except MODULE.RegistrationDeferred as deferred:
        assert deferred.registration is second
    else:
        raise AssertionError("exhausted retry chain was not deferred")
