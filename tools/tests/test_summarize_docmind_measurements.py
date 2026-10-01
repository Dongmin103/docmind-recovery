import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from summarize_docmind_measurements import summarize


def record(file_id, attempt=1, status="COMPLETE", parse_ms=100, size=1048576,
           chunks=2, extension=".doc", cold=False, ocr=False, converted=True,
           cohort="synthetic"):
    return {
        "file_id": file_id,
        "cohort": cohort,
        "extension": extension,
        "bytes": size,
        "chunk_count": chunks,
        "attempt": attempt,
        "status": status,
        "cold": cold,
        "ocr": ocr,
        "converted": converted,
        "error_code": None,
        "failed_stage": None,
        "cleanup_status": "COMPLETE",
        "durations_ns": {
            "parse_http": parse_ms * 1000000,
            "normalize_artifacts": 10000000,
            "chunk": 20000000,
            "embedding": 30000000,
            "index_staging": 40000000,
            "activation": 5000000,
            "end_to_end": (parse_ms + 110) * 1000000,
        },
        "verification": {
            "job_status": "COMPLETE",
            "parser_run_status": "READY",
            "active_chunk_set_matches": True,
            "es_active_chunk_count": chunks,
            "expected_active_chunk_count": chunks,
            "host_cleanup": "COMPLETE",
            "container_cleanup": "COMPLETE",
        },
    }


class SummaryTests(unittest.TestCase):
    def test_first_attempt_verified_success_distribution_and_redaction(self):
        rows = [record("opaque-a", parse_ms=100), record("opaque-b", parse_ms=200),
                record("opaque-c", parse_ms=300)]
        result = summarize(rows)
        group = result["first_attempt_success"][0]
        self.assertEqual(group["dimensions"], {"cohort": "synthetic", "extension": ".doc", "temperature": "warm", "ocr": False, "converted": True})
        parse = group["metrics_ms"]["parse_http"]
        self.assertEqual(parse["count"], 3)
        self.assertEqual(parse["sum"], 600)
        self.assertEqual(parse["median"], 200)
        self.assertEqual(parse["p90"], 280)
        self.assertEqual(parse["p95"], 290)
        self.assertEqual(parse["max"], 300)
        self.assertTrue(parse["p95_reference_only"])
        self.assertEqual(group["metrics_ms"]["parse_and_chunk"]["median"], 230)
        self.assertEqual(group["metrics_ms"]["index"]["median"], 75)
        self.assertEqual(parse["ms_per_mib"], 200)
        self.assertEqual(parse["ms_per_chunk"], 100)
        serialized = json.dumps(result)
        self.assertNotIn("opaque-a", serialized)
        self.assertNotIn("file_id", serialized)

    def test_retries_failures_exclusions_and_invalid_success_stay_out_of_distribution(self):
        retried = record("retry", attempt=2)
        failed = record("failure", status="FAILED")
        failed["error_code"] = "PARSE_FAILED"
        failed["failed_stage"] = "parse_http"
        failed["cleanup_status"] = "COMPLETE"
        excluded = record("excluded", status="EXCLUDED", extension=".pdf")
        excluded["error_code"] = "PLACEHOLDER"
        excluded["failed_stage"] = "source_metadata"
        excluded["cleanup_status"] = "NOT_STARTED"
        invalid = record("invalid")
        invalid["verification"]["es_active_chunk_count"] = 1
        result = summarize([record("good"), retried, failed, excluded, invalid])
        self.assertEqual(result["first_attempt_success"][0]["count"], 1)
        self.assertEqual(result["retry_success_count"], 1)
        self.assertEqual(result["unverified_complete_count"], 1)
        self.assertEqual(result["failure_counts"], [
            {"cohort": "synthetic", "extension": ".doc", "error_code": "PARSE_FAILED", "stage": "parse_http", "attempt": 1, "cleanup_status": "COMPLETE", "count": 1}
        ])
        self.assertEqual(result["exclusion_counts"][0]["error_code"], "PLACEHOLDER")

    def test_missing_timing_does_not_become_zero_and_duplicate_attempt_is_rejected(self):
        incomplete = record("a")
        del incomplete["durations_ns"]["chunk"]
        result = summarize([incomplete])
        self.assertEqual(result["first_attempt_success"], [])
        self.assertEqual(result["unverified_complete_count"], 1)
        with self.assertRaises(ValueError):
            summarize([record("a"), record("a")])

    def test_missing_classification_cannot_silently_join_warm_non_ocr_group(self):
        incomplete = record("a")
        del incomplete["cold"]
        result = summarize([incomplete])
        self.assertEqual(result["first_attempt_success"], [])
        self.assertEqual(result["unverified_complete_count"], 1)

    def test_optional_timing_coverage_reports_missing_values(self):
        measured = record("a")
        measured["durations_ns"]["hydration"] = 0
        result = summarize([measured, record("b")])
        coverage = result["first_attempt_success"][0]["optional_timing_coverage"]
        self.assertEqual(coverage["hydration"], {"measured": 1, "missing": 1})
        self.assertEqual(coverage["queue_wait"], {"measured": 0, "missing": 2})

    def test_synthetic_and_real_cohorts_never_share_a_distribution(self):
        result = summarize([record("synthetic"), record("real", cohort="real_t")])
        self.assertEqual(len(result["first_attempt_success"]), 2)
        self.assertEqual({group["dimensions"]["cohort"] for group in result["first_attempt_success"]}, {"synthetic", "real_t"})


if __name__ == "__main__":
    unittest.main()
