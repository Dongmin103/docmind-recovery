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
