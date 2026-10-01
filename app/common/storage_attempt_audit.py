"""Optional, content-free storage call audit for cloud ingestion attempts."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from time import perf_counter_ns


_METHODS = ("get", "put", "delete", "rm", "obj_exist", "health")


class StorageAttemptAudit:
    def __init__(self, storage, root: str | Path):
        self._storage = storage
        self._root = Path(root)
        self._active = ContextVar("docmind_storage_attempt", default=None)

    def __getattr__(self, name):
        target = getattr(self._storage, name)
        if name.startswith("_") or not callable(target):
            return target

        @wraps(target)
        def audited(*args, **kwargs):
            state = self._active.get()
            if state is not None:
                with state["lock"]:
                    counts = state["methods"]
                    counts[name] = counts.get(name, 0) + 1
            return target(*args, **kwargs)

        return audited

    @contextmanager
    def attempt(self, job_id: str, fencing_token: int):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("storage audit requires an opaque job ID")
        if not isinstance(fencing_token, int) or fencing_token < 0:
            raise ValueError("storage audit requires a nonnegative fence")
        if not self._root.is_dir():
            raise ValueError("storage audit root is unavailable")
        state = {"lock": threading.Lock(), "methods": dict.fromkeys(_METHODS, 0), "stages": []}
        token = self._active.set(state)
        started_ns = perf_counter_ns()
        started_utc = datetime.now(UTC).isoformat()
        status = "ok"
        try:
            yield
        except BaseException:
            status = "error"
            raise
        finally:
            self._active.reset(token)
            record = {
                "job_id": job_id,
                "fencing_token": fencing_token,
                "attempt_id": uuid.uuid4().hex,
                "started_utc": started_utc,
                "finished_utc": datetime.now(UTC).isoformat(),
                "duration_ns": perf_counter_ns() - started_ns,
                "status": status,
                "methods": state["methods"],
                "stages": state["stages"],
            }
            destination = self._root / f"{job_id}-{fencing_token}-{record['attempt_id']}.json"
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")

    @contextmanager
    def stage(self, name: str, parse_run_id: str | None = None, *, job_id: str | None = None):
        identifier = parse_run_id if job_id is None else job_id
        if (
            not re.fullmatch(r"[a-z_]{1,32}", name)
            or (parse_run_id is None) == (job_id is None)
            or not isinstance(identifier, str)
            or not re.fullmatch(r"[0-9a-f]{32}", identifier)
        ):
            raise ValueError("storage audit stage requires safe identifiers")
        state = self._active.get()
        if state is None:
            yield
            return
        started_ns = perf_counter_ns()
        status = "ok"
        try:
            yield
        except BaseException:
            status = "error"
            raise
        finally:
            stage = {
                "name": name,
                "job_id" if job_id is not None else "parse_run_id": identifier,
                "duration_ns": perf_counter_ns() - started_ns,
                "status": status,
            }
            with state["lock"]:
                state["stages"].append(stage)


def stage_scope(storage, name: str, parse_run_id: str):
    if isinstance(storage, StorageAttemptAudit):
        return storage.stage(name, parse_run_id)
    return nullcontext()


def job_stage_scope(storage, name: str, job_id: str):
    if isinstance(storage, StorageAttemptAudit):
        return storage.stage(name, job_id=job_id)
    return nullcontext()
