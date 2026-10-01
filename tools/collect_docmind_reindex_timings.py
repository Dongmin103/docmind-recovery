"""Join private DEPT2 DB status, app stage audits, and host timing records.

The export contains opaque job IDs and metrics only. No relative paths, text,
secrets, or error messages are read or written by this tool.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


FIELDS = (
    "job_id", "extension", "ciphertext_bytes", "status", "attempt", "error_code",
    "host_cleanup_state", "container_cleanup_state", "parser_run_state",
    "chunk_count", "token_count", "queue_wait_s", "source_verify_s",
    "decrypt_s", "post_decrypt_verify_s", "delivery_roundtrip_s",
    "parse_http_s", "normalize_artifacts_s", "chunk_s", "embedding_s",
    "index_staging_s", "activation_s", "container_cleanup_s",
    "app_total_s", "host_cleanup_s", "status_ack_s", "host_total_s",
    "app_wall_s", "host_wall_s", "app_clock_gap_s", "host_clock_gap_s",
    "observation_to_last_update_s", "audit_status", "host_state",
)
DB_FIELDS = (
    "job_id", "extension", "ciphertext_bytes", "status", "attempt", "fence",
    "error_code", "host_cleanup_state", "container_cleanup_state", "created",
    "updated", "parser_run_state", "chunk_count", "token_count",
)
APP_STAGES = (
    "parse_http", "normalize_artifacts", "chunk", "embedding",
    "index_staging", "activation", "container_cleanup",
)
HOST_STAGES = (
    "source_verify", "decrypt", "post_decrypt_verify", "delivery_roundtrip",
    "host_cleanup", "status_ack", "host_total",
)


def seconds(value):
    return round(value / 1_000_000_000, 3) if isinstance(value, int) else ""


def wall_seconds(record):
    try:
        started = datetime.fromisoformat(record["started_utc"])
        finished = datetime.fromisoformat(record["finished_utc"])
        elapsed = (finished - started).total_seconds()
        return round(elapsed, 3) if elapsed >= 0 else ""
    except (KeyError, TypeError, ValueError):
        return ""


def read_records(directory: Path):
    records = []
    if not directory.is_dir():
        return records
    for path in directory.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            record["fencing_token"] = int(record["fencing_token"])
            if len(record["job_id"]) != 32:
                continue
            records.append((path.stat().st_mtime_ns, record))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return records


def latest_by_job(records):
    selected = {}
    for modified, record in records:
        key = (record["job_id"], record["fencing_token"])
        if key not in selected or modified > selected[key][0]:
            selected[key] = (modified, record)
    return {key: value[1] for key, value in selected.items()}


def join(db_path: Path, app_dir: Path, host_dir: Path):
    app = latest_by_job(read_records(app_dir))
    host = latest_by_job(read_records(host_dir))
    output = []
    with db_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, fieldnames=DB_FIELDS, delimiter="\t")
        for db in reader:
            if not db["job_id"]:
                continue
            key = (db["job_id"], int(db["fence"]))
            attempt_app = app.get(key, {})
            attempt_host = host.get(key, {})
            stages = {stage["name"]: stage for stage in attempt_app.get("stages", [])}
            host_durations = attempt_host.get("durations_ns", {})
            row = {field: "" for field in FIELDS}
            row.update({
                "job_id": db["job_id"],
                "extension": db["extension"],
                "ciphertext_bytes": db["ciphertext_bytes"],
                "status": db["status"],
                "attempt": db["attempt"],
                "error_code": "" if db["error_code"] == "NULL" else db["error_code"],
                "host_cleanup_state": db["host_cleanup_state"],
                "container_cleanup_state": db["container_cleanup_state"],
                "parser_run_state": "" if db["parser_run_state"] == "NULL" else db["parser_run_state"],
                "chunk_count": "" if db["chunk_count"] == "NULL" else db["chunk_count"],
                "token_count": "" if db["token_count"] == "NULL" else db["token_count"],
                "app_total_s": seconds(attempt_app.get("duration_ns")),
                "app_wall_s": wall_seconds(attempt_app),
                "host_wall_s": wall_seconds(attempt_host),
                "audit_status": attempt_app.get("status", ""),
                "host_state": attempt_host.get("state", ""),
            })
            for stage in APP_STAGES:
                row[stage + "_s"] = seconds(stages.get(stage, {}).get("duration_ns"))
            for stage in HOST_STAGES:
                row[stage + "_s"] = seconds(host_durations.get(stage))
            for name in ("app", "host"):
                elapsed = row[name + "_total_s"]
                wall = row[name + "_wall_s"]
                if elapsed != "" and wall != "":
                    row[name + "_clock_gap_s"] = round(elapsed - wall, 3)
            if attempt_host.get("started_utc") and db["created"] != "NULL":
                # App DB timestamps are naive local KST; host records are UTC.
                from zoneinfo import ZoneInfo

                created = datetime.fromisoformat(db["created"]).replace(tzinfo=ZoneInfo("Asia/Seoul"))
                started = datetime.fromisoformat(attempt_host["started_utc"])
                row["queue_wait_s"] = round((started - created).total_seconds(), 3)
            if db["created"] != "NULL" and db["updated"] != "NULL":
                created = datetime.fromisoformat(db["created"])
                updated = datetime.fromisoformat(db["updated"])
                row["observation_to_last_update_s"] = round((updated - created).total_seconds(), 3)
            output.append(row)
    return output


def errors(db_rows, app_dir: Path, host_dir: Path):
    by_job = {row["job_id"]: row for row in db_rows}
    app_records = latest_by_job(read_records(app_dir))
    host_records = latest_by_job(read_records(host_dir))
    output = []
    for key in sorted(set(app_records) | set(host_records)):
        if key[0] not in by_job:
            continue
        app = app_records.get(key, {})
        host = host_records.get(key, {})
        if app.get("status") != "error" and host.get("state") not in ("FAILED", "CLEANUP_FAILED"):
            continue
        db = by_job.get(key[0], {})
        failed_stages = sorted({stage["name"] for stage in app.get("stages", [])
                                if stage.get("status") == "error"})
        output.append({
            "job_id": key[0],
            "fencing_token": key[1],
            "extension": host.get("extension") or db.get("extension", ""),
            "error_code": host.get("error_code") or db.get("error_code") or "APP_ATTEMPT_ERROR",
            "failed_stages": ",".join(failed_stages),
            "app_status": app.get("status", ""),
            "host_state": host.get("state", ""),
            "app_total_s": seconds(app.get("duration_ns")),
            "host_total_s": seconds(host.get("durations_ns", {}).get("host_total")),
        })
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--app-audit", type=Path, required=True)
    parser.add_argument("--host-timing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--errors", type=Path)
    args = parser.parse_args()
    rows = join(args.db, args.app_audit, args.host_timing)
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    if args.errors:
        failures = errors(rows, args.app_audit, args.host_timing)
        with args.errors.open("w", encoding="utf-8", newline="") as stream:
            fields = ("job_id", "fencing_token", "extension", "error_code", "failed_stages",
                      "app_status", "host_state", "app_total_s", "host_total_s")
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(failures)
    print(json.dumps({"jobs": len(rows), "states": Counter(row["status"] for row in rows),
                      "errors": Counter(row["error_code"] for row in rows if row["error_code"])},
                     sort_keys=True))


if __name__ == "__main__":
    main()
