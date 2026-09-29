"""Aggregate sanitized, joined DocMind ingestion attempt records.

The input is a private JSON array with one record per (opaque file_id, attempt).
Each record supplies cohort (synthetic or real_t), extension, bytes,
chunk_count, attempt, status, cold/ocr/converted flags, durations_ns, and
verification. Verification must contain the
MySQL job and parser-run states, active chunk-set match, ES actual/expected
counts, and both cleanup states. The output contains only grouped statistics.
The caller must join the private source manifest, stage audit, DB/ES state and
cleanup receipts before using this tool; a stage audit alone is insufficient.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path


REQUIRED_STAGES = (
    "parse_http", "normalize_artifacts", "chunk", "embedding",
    "index_staging", "activation", "end_to_end",
)
OPTIONAL_STAGES = ("hydration", "queue_wait", "model_cold_start", "external_api")
METRICS = (*REQUIRED_STAGES, "parse_and_chunk", "index")
SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SAFE_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,12}$")


def _percentile(values: list[float], fraction: float) -> float:
    rank = (len(values) - 1) * fraction
    lower, upper = math.floor(rank), math.ceil(rank)
    return values[lower] + (values[upper] - values[lower]) * (rank - lower)


def _round(value: float) -> float:
    return round(value, 3)


def _safe_code(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and SAFE_CODE.fullmatch(value) else fallback


def _safe_extension(value: object) -> str:
    return value if isinstance(value, str) and SAFE_EXTENSION.fullmatch(value) else ".unknown"


def _safe_stage(value: object) -> str:
    return value if isinstance(value, str) and SAFE_STAGE.fullmatch(value) else "unclassified"


def _verified(record: dict) -> bool:
    if record.get("status") != "COMPLETE":
        return False
    if record.get("cohort") not in ("synthetic", "real_t"):
        return False
    if any(type(record.get(name)) is not bool for name in ("cold", "ocr", "converted")):
        return False
    if _safe_extension(record.get("extension")) == ".unknown":
        return False
    check = record.get("verification")
    if not isinstance(check, dict):
        return False
    if not (
        check.get("job_status") == "COMPLETE"
        and check.get("parser_run_status") == "READY"
        and check.get("active_chunk_set_matches") is True
        and check.get("host_cleanup") == "COMPLETE"
        and check.get("container_cleanup") == "COMPLETE"
    ):
        return False
    expected = check.get("expected_active_chunk_count")
    actual = check.get("es_active_chunk_count")
    if type(expected) is not int or type(actual) is not int or expected <= 0 or expected != actual:
        return False
    if type(record.get("bytes")) is not int or record["bytes"] <= 0:
        return False
    if type(record.get("chunk_count")) is not int or record["chunk_count"] != expected:
        return False
    durations = record.get("durations_ns")
    if not isinstance(durations, dict):
        return False
    if any(type(durations.get(stage)) is not int or durations[stage] < 0 for stage in REQUIRED_STAGES):
        return False
    measured_sum = sum(durations[stage] for stage in REQUIRED_STAGES if stage != "end_to_end")
    return measured_sum <= durations["end_to_end"]


def _dimensions(record: dict) -> tuple:
    return (
        record["cohort"],
        _safe_extension(record.get("extension")),
        "cold" if record.get("cold") is True else "warm",
        record.get("ocr") is True,
        record.get("converted") is True,
    )


def _aggregate(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        groups[_dimensions(row)].append(row)
    result = []
    for key, members in sorted(groups.items()):
        total_bytes = sum(row["bytes"] for row in members)
        total_chunks = sum(row["chunk_count"] for row in members)
        sample_by_metric = defaultdict(list)
        optional_coverage = {name: 0 for name in OPTIONAL_STAGES}
        for row in members:
            durations = row["durations_ns"]
            for name in REQUIRED_STAGES:
                sample_by_metric[name].append(durations[name] / 1_000_000)
            sample_by_metric["parse_and_chunk"].append(
                sum(durations[name] for name in ("parse_http", "normalize_artifacts", "chunk")) / 1_000_000
            )
            sample_by_metric["index"].append(
                sum(durations[name] for name in ("embedding", "index_staging", "activation")) / 1_000_000
            )
            for name in OPTIONAL_STAGES:
                value = durations.get(name)
                if type(value) is int and value >= 0:
                    optional_coverage[name] += 1
        metrics = {}
        for name in METRICS:
            values = sorted(sample_by_metric[name])
            total = sum(values)
            metrics[name] = {
                "count": len(values),
                "sum": _round(total),
                "median": _round(_percentile(values, .5)),
                "p90": _round(_percentile(values, .9)),
                "p95": _round(_percentile(values, .95)),
                "p95_reference_only": len(values) < 20,
                "max": _round(values[-1]),
                "ms_per_mib": _round(total / (total_bytes / 1048576)),
                "ms_per_chunk": _round(total / total_chunks),
            }
        result.append({
            "dimensions": dict(zip(("cohort", "extension", "temperature", "ocr", "converted"), key)),
            "count": len(members),
            "total_bytes": total_bytes,
            "total_chunks": total_chunks,
            "metrics_ms": metrics,
            "optional_timing_coverage": {
                name: {"measured": count, "missing": len(members) - count}
                for name, count in optional_coverage.items()
            },
        })
    return result


def _count_by_fields(rows: list[dict]) -> list[dict]:
    counts = Counter()
    for row in rows:
        counts[(
            row.get("cohort") if row.get("cohort") in ("synthetic", "real_t") else "unknown",
            _safe_extension(row.get("extension")),
            _safe_code(row.get("error_code"), "UNCLASSIFIED"),
            _safe_stage(row.get("failed_stage")),
            row["attempt"],
            _safe_code(row.get("cleanup_status"), "UNKNOWN"),
        )] += 1
    return [
        dict(zip(("cohort", "extension", "error_code", "stage", "attempt", "cleanup_status"), key), count=count)
        for key, count in sorted(counts.items())
    ]


def summarize(records: list[dict]) -> dict:
    if not isinstance(records, list):
        raise ValueError("Input must be a JSON array of attempt records.")
    seen = set()
    for row in records:
        if not isinstance(row, dict) or not isinstance(row.get("file_id"), str) or not row["file_id"]:
            raise ValueError("Each attempt needs an opaque file ID.")
        attempt = row.get("attempt")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("Attempt must be a positive integer.")
        key = (row["file_id"], attempt)
        if key in seen:
            raise ValueError("Duplicate file attempt.")
        seen.add(key)
    first = [row for row in records if row["attempt"] == 1 and _verified(row)]
    retries = [row for row in records if row["attempt"] > 1 and _verified(row)]
    unverified = [row for row in records if row.get("status") == "COMPLETE" and not _verified(row)]
    failures = [row for row in records if row.get("status") == "FAILED"]
    exclusions = [row for row in records if row.get("status") == "EXCLUDED"]
    return {
        "schema_version": 1,
        "input_attempt_count": len(records),
        "first_attempt_success": _aggregate(first),
        "retry_success": _aggregate(retries),
        "retry_success_count": len(retries),
        "unverified_complete_count": len(unverified),
        "failure_counts": _count_by_fields(failures),
        "exclusion_counts": _count_by_fields(exclusions),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Private joined attempt JSON array")
    parser.add_argument("--output", type=Path, required=True, help="Aggregate-only summary JSON")
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Input and output must differ.")
    records = json.loads(args.input.read_text(encoding="utf-8"))
    result = summarize(records)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
