import json

import pytest

from common.storage_attempt_audit import StorageAttemptAudit


class Storage:
    def get(self, bucket, key):
        return b"private content"

    def put(self, bucket, key, content):
        raise RuntimeError("storage unavailable")

    def health(self):
        return True


def test_records_attempts_before_storage_errors_without_arguments(tmp_path):
    audited = StorageAttemptAudit(Storage(), tmp_path)

    with audited.attempt("a" * 32, 3):
        assert audited.get("secret-bucket", "secret-key") == b"private content"
        with pytest.raises(RuntimeError, match="storage unavailable"):
            audited.put("secret-bucket", "secret-key", b"private content")
        assert audited.health() is True

    reports = list(tmp_path.glob("*.json"))
    assert len(reports) == 1
    raw = reports[0].read_text(encoding="utf-8")
    report = json.loads(raw)
    assert report["job_id"] == "a" * 32
    assert report["fencing_token"] == 3
    assert report["methods"] == {
        "get": 1, "put": 1, "delete": 0, "rm": 0, "obj_exist": 0, "health": 1
    }
    assert "secret" not in raw
    assert "private content" not in raw


def test_failed_job_still_records_method_attempts(tmp_path):
    audited = StorageAttemptAudit(Storage(), tmp_path)

    with pytest.raises(RuntimeError, match="storage unavailable"):
        with audited.attempt("b" * 32, 4):
            audited.put("secret-bucket", "secret-key", b"private content")

    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert report["methods"]["put"] == 1
    assert report["status"] == "error"


def test_stage_timer_records_failed_partial_duration(tmp_path):
    audited = StorageAttemptAudit(Storage(), tmp_path)

    with audited.attempt("c" * 32, 5):
        with audited.stage("parse_http", "d" * 32):
            pass
        with pytest.raises(RuntimeError, match="parser failed"):
            with audited.stage("normalize_artifacts", "d" * 32):
                raise RuntimeError("parser failed")

    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    assert [stage["name"] for stage in report["stages"]] == ["parse_http", "normalize_artifacts"]
    assert [stage["status"] for stage in report["stages"]] == ["ok", "error"]
    assert all(stage["duration_ns"] >= 0 for stage in report["stages"])
    assert all(stage["parse_run_id"] == "d" * 32 for stage in report["stages"])
    assert sum(stage["duration_ns"] for stage in report["stages"]) <= report["duration_ns"]
    assert report["started_utc"] <= report["finished_utc"]
