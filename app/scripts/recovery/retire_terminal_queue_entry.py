#!/usr/bin/env python3
"""Fail-closed cleanup for one terminal Redis task-stream entry.

This script is intentionally recovery-only. It never reads from, claims from,
or acknowledges a consumer group. The only permitted mutation is an atomic Lua
guard that deletes exactly one stream entry with XDEL after rechecking that the
entry is the next undelivered item for the configured group.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULTS = {
    "mysql_container": "docker-mysql-1",
    "mysql_database": "rag_flow",
    "mysql_user": "root",
    "mysql_password": "infini_rag_flow",
    "redis_container": "docker-redis-1",
    "redis_db": "1",
    "redis_password": "infini_rag_flow",
    "stream": "te.0.common",
    "group": "rag_flow_svr_task_broker",
    "stream_id": "1786495923662-0",
    "kb_id": "fff626ae95e711f1a9207b94957268ce",
    "task_id": "08ae899495e811f1a9207b94957268ce",
    "doc_id": "0206199095e811f1a9207b94957268ce",
    "from_page": 12,
    "to_page": 24,
}


LUA_GUARDED_XDEL = r"""
local stream = KEYS[1]
local group = ARGV[1]
local expected_id = ARGV[2]
local expected_task = ARGV[3]
local expected_doc = ARGV[4]
local expected_from = tonumber(ARGV[5])
local expected_to = tonumber(ARGV[6])

local group_info = redis.call('XINFO', 'GROUPS', stream)
local last_delivered = nil
for _, g in ipairs(group_info) do
  local name = nil
  local group_last = nil
  for i = 1, #g, 2 do
    if g[i] == 'name' then name = g[i + 1] end
    if g[i] == 'last-delivered-id' then group_last = g[i + 1] end
  end
  if name == group then
    last_delivered = group_last
  end
end

if not last_delivered then
  return cjson.encode({ok=false, reason='group_not_found'})
end

local next_entries = redis.call('XRANGE', stream, '(' .. last_delivered, '+', 'COUNT', 1)
if #next_entries ~= 1 then
  return cjson.encode({ok=false, reason='no_next_entry', last_delivered_id=last_delivered})
end

local entry = next_entries[1]
local actual_id = entry[1]
if actual_id ~= expected_id then
  return cjson.encode({
    ok=false,
    reason='next_entry_mismatch',
    expected_id=expected_id,
    actual_id=actual_id,
    last_delivered_id=last_delivered
  })
end

local fields = entry[2]
local message = nil
for i = 1, #fields, 2 do
  if fields[i] == 'message' then
    message = fields[i + 1]
  end
end
if not message then
  return cjson.encode({ok=false, reason='missing_message', entry_id=actual_id})
end

local ok, payload = pcall(cjson.decode, message)
if not ok then
  return cjson.encode({ok=false, reason='invalid_json', entry_id=actual_id})
end

if payload['id'] ~= expected_task then
  return cjson.encode({ok=false, reason='task_id_mismatch', entry_id=actual_id})
end
if payload['doc_id'] ~= expected_doc then
  return cjson.encode({ok=false, reason='doc_id_mismatch', entry_id=actual_id})
end
if tonumber(payload['from_page']) ~= expected_from then
  return cjson.encode({ok=false, reason='from_page_mismatch', entry_id=actual_id})
end
if tonumber(payload['to_page']) ~= expected_to then
  return cjson.encode({ok=false, reason='to_page_mismatch', entry_id=actual_id})
end

local deleted = redis.call('XDEL', stream, expected_id)
return cjson.encode({
  ok = deleted == 1,
  reason = deleted == 1 and 'deleted' or 'xdel_returned_non_one',
  deleted = deleted,
  entry_id = actual_id,
  last_delivered_id = last_delivered,
  payload = payload
})
"""


def run(cmd: list[str], *, input_text: str | None = None) -> str:
    proc = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            json.dumps(
                {
                    "cmd": redact(cmd),
                    "returncode": proc.returncode,
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                },
                indent=2,
            )
        )
    return proc.stdout


def redact(cmd: list[str]) -> list[str]:
    redacted = []
    for part in cmd:
        if part.startswith("-p") and len(part) > 2:
            redacted.append("-p***")
        else:
            redacted.append(part)
    return redacted


def mysql(args: argparse.Namespace, sql: str) -> str:
    return run(
        [
            "docker",
            "exec",
            args.mysql_container,
            "mysql",
            f"-u{args.mysql_user}",
            f"-p{args.mysql_password}",
            "-N",
            "-B",
            args.mysql_database,
            "-e",
            sql,
        ]
    )


def redis_cli(args: argparse.Namespace, redis_args: list[str], *, input_text: str | None = None) -> str:
    return run(
        [
            "docker",
            "exec",
            "-i",
            args.redis_container,
            "redis-cli",
            "-n",
            str(args.redis_db),
            "-a",
            args.redis_password,
            "--no-auth-warning",
            "--raw",
            *redis_args,
        ],
        input_text=input_text,
    )


def parse_info(raw: str) -> dict[str, Any]:
    lines = [line for line in raw.splitlines() if line != ""]
    return {lines[i]: lines[i + 1] for i in range(0, len(lines) - 1, 2)}


def parse_groups(raw: str) -> list[dict[str, Any]]:
    lines = [line for line in raw.splitlines() if line != ""]
    groups: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    i = 0
    while i < len(lines) - 1:
        key, value = lines[i], lines[i + 1]
        if key == "name" and current:
            groups.append(current)
            current = {}
        current[key] = value
        i += 2
    if current:
        groups.append(current)
    return groups


def parse_xrange_one(raw: str) -> dict[str, Any] | None:
    lines = [line for line in raw.splitlines() if line != ""]
    if not lines:
        return None
    entry_id = lines[0]
    fields = {lines[i]: lines[i + 1] for i in range(1, len(lines) - 1, 2)}
    parsed = {"id": entry_id, "fields": fields}
    if "message" in fields:
        try:
            parsed["message_json"] = json.loads(fields["message"])
        except json.JSONDecodeError:
            parsed["message_json_error"] = "invalid_json"
    return parsed


def parse_db_row(raw: str) -> dict[str, Any] | None:
    rows = [line.split("\t") for line in raw.splitlines() if line and not line.startswith("mysql:")]
    if len(rows) != 1:
        return None
    cols = [
        "task_id",
        "doc_id",
        "from_page",
        "to_page",
        "task_progress",
        "document_id",
        "kb_id",
        "document_progress",
        "run",
        "status",
        "chunk_num",
        "name",
    ]
    return dict(zip(cols, rows[0], strict=True))


def db_target_state(args: argparse.Namespace) -> dict[str, Any] | None:
    sql = f"""
SELECT
  t.id,t.doc_id,t.from_page,t.to_page,t.progress,
  d.id,d.kb_id,d.progress,d.run,d.status,d.chunk_num,d.name
FROM task t
JOIN document d ON d.id=t.doc_id
WHERE t.id='{args.task_id}'
  AND d.id='{args.doc_id}'
  AND d.kb_id='{args.kb_id}';
"""
    return parse_db_row(mysql(args, sql))


def active_count(args: argparse.Namespace) -> int:
    sql = f"""
SELECT COUNT(*)
FROM task t
JOIN document d ON d.id=t.doc_id
WHERE d.kb_id='{args.kb_id}' AND t.progress < 1;
"""
    raw = mysql(args, sql)
    lines = [line for line in raw.splitlines() if line and not line.startswith("mysql:")]
    if len(lines) != 1:
        raise RuntimeError(f"unexpected active count output: {raw!r}")
    return int(lines[0])


def assert_terminal(args: argparse.Namespace, state: dict[str, Any] | None) -> None:
    if state is None:
        raise RuntimeError("target DB task/document row not found or not unique")
    expected = {
        "task_id": args.task_id,
        "doc_id": args.doc_id,
        "from_page": str(args.from_page),
        "to_page": str(args.to_page),
        "document_id": args.doc_id,
        "kb_id": args.kb_id,
        "task_progress": "1",
        "status": "1",
    }
    mismatches = {k: {"expected": v, "actual": state.get(k)} for k, v in expected.items() if state.get(k) != v}
    if mismatches:
        raise RuntimeError(json.dumps({"reason": "db_terminal_guard_failed", "mismatches": mismatches}, indent=2))


def guarded_xdel(args: argparse.Namespace) -> dict[str, Any]:
    raw = redis_cli(
        args,
        [
            "EVAL",
            LUA_GUARDED_XDEL,
            "1",
            args.stream,
            args.group,
            args.stream_id,
            args.task_id,
            args.doc_id,
            str(args.from_page),
            str(args.to_page),
        ],
    )
    result = json.loads(raw)
    if not result.get("ok") or result.get("deleted") != 1:
        raise RuntimeError(json.dumps({"reason": "redis_guard_failed", "result": result}, indent=2))
    return result


def build_report(args: argparse.Namespace, execute: bool) -> dict[str, Any]:
    before_db = db_target_state(args)
    before_active = active_count(args)
    before_stream = parse_info(redis_cli(args, ["XINFO", "STREAM", args.stream]))
    before_groups = parse_groups(redis_cli(args, ["XINFO", "GROUPS", args.stream]))
    before_entry = parse_xrange_one(redis_cli(args, ["XRANGE", args.stream, args.stream_id, args.stream_id]))

    assert_terminal(args, before_db)

    if before_entry is None:
        raise RuntimeError("target Redis stream entry is missing")

    if not execute:
        mutation: dict[str, Any] = {"executed": False, "reason": "dry_run"}
    else:
        mutation = {"executed": True, **guarded_xdel(args)}

    after_db = db_target_state(args)
    after_active = active_count(args)
    after_stream = parse_info(redis_cli(args, ["XINFO", "STREAM", args.stream]))
    after_groups = parse_groups(redis_cli(args, ["XINFO", "GROUPS", args.stream]))
    after_entry = parse_xrange_one(redis_cli(args, ["XRANGE", args.stream, args.stream_id, args.stream_id]))

    invariant = {
        "active_task_count_before": before_active,
        "active_task_count_after": after_active,
        "unchanged": before_active == after_active,
    }
    if not invariant["unchanged"]:
        raise RuntimeError(json.dumps({"reason": "active_invariant_changed", "invariant": invariant}, indent=2))

    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "mode": "execute" if execute else "dry_run",
        "target": {
            "kb_id": args.kb_id,
            "task_id": args.task_id,
            "doc_id": args.doc_id,
            "from_page": args.from_page,
            "to_page": args.to_page,
            "stream": args.stream,
            "group": args.group,
            "stream_id": args.stream_id,
        },
        "before": {
            "db_target": before_db,
            "active_invariant_count": before_active,
            "stream_info": before_stream,
            "groups": before_groups,
            "entry": before_entry,
        },
        "mutation": mutation,
        "after": {
            "db_target": after_db,
            "active_invariant_count": after_active,
            "stream_info": after_stream,
            "groups": after_groups,
            "entry": after_entry,
        },
        "invariant": invariant,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    required_target_args = {"stream_id", "kb_id", "task_id", "doc_id", "from_page", "to_page"}
    for key, value in DEFAULTS.items():
        flag = "--" + key.replace("_", "-")
        kwargs: dict[str, Any] = {} if key in required_target_args else {"default": value}
        if key in required_target_args:
            kwargs["required"] = True
        if isinstance(value, int):
            kwargs["type"] = int
        p.add_argument(flag, **kwargs)
    p.add_argument("--execute", action="store_true", help="perform the guarded XDEL")
    p.add_argument("--report", default="", help="optional JSON report path")
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        report = build_report(args, execute=args.execute)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1

    report["ok"] = True
    output = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
