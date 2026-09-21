#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO / "runtime/private/docmind-openviking-capability.json"
BASE_URL = "http://127.0.0.1:1933"
CONTAINER = "openviking-phase1"
EXPECTED_ENTRY_SUFFIXES = frozenset(
    {
        ".abstract.md",
        ".overview.md",
        "probe",
        "probe/manifest.md",
        "probe/manifest.md/manifest.md",
        "probe/.abstract.md",
        "probe/.overview.md",
        "probe/manifest.md/.abstract.md",
        "probe/manifest.md/.overview.md",
    }
)


class CapabilityError(RuntimeError):
    pass


class _HTTPResponse:
    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self.body = body

    def json(self) -> dict[str, Any]:
        value = json.loads(self.body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("response JSON must be an object")
        return value


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    body: bytes | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: int,
) -> _HTTPResponse:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request_headers = dict(headers)
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return _HTTPResponse(response.status, response.read())


def _multipart_upload_body(content: bytes) -> tuple[bytes, str]:
    boundary = f"docmind-{uuid4().hex}"
    marker = boundary.encode("ascii")
    parts = [
        b"--" + marker + b'\r\nContent-Disposition: form-data; name="telemetry"\r\n\r\nfalse\r\n',
        b"--" + marker + b'\r\nContent-Disposition: form-data; name="upload_mode"\r\n\r\nlocal\r\n',
        (
            b"--"
            + marker
            + b'\r\nContent-Disposition: form-data; name="file"; filename="manifest.md"\r\n'
            + b"Content-Type: text/markdown\r\n\r\n"
            + content
            + b"\r\n"
        ),
        b"--" + marker + b"--\r\n",
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_json(value: Any) -> str:
    return _sha_bytes(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )


class OpenVikingClient:
    def __init__(self, base_url: str, api_key: str):
        if not api_key:
            raise CapabilityError("OpenViking API key is empty")
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key}
        self.calls: list[str] = []

    def _result(self, response: _HTTPResponse, operation: str) -> Any:
        self.calls.append(operation)
        try:
            payload = response.json()
        except Exception as error:
            raise CapabilityError(f"{operation} returned non-JSON") from error
        if response.status_code >= 400 or payload.get("status") != "ok":
            raise CapabilityError(f"{operation} failed with HTTP {response.status_code}")
        return payload.get("result")

    def get(self, path: str, **query: Any) -> Any:
        response = _request(
            "GET",
            f"{self.base_url}{path}",
            headers=self.headers,
            params=query,
            timeout=30,
        )
        return self._result(response, path)

    def temp_upload(self, content: bytes) -> str:
        body, content_type = _multipart_upload_body(content)
        response = _request(
            "POST",
            f"{self.base_url}/api/v1/resources/temp_upload",
            headers={**self.headers, "Content-Type": content_type},
            body=body,
            timeout=60,
        )
        result = self._result(response, "/api/v1/resources/temp_upload")
        temp_file_id = result.get("temp_file_id") if isinstance(result, dict) else None
        if not isinstance(temp_file_id, str) or not temp_file_id:
            raise CapabilityError("temp upload returned no temp_file_id")
        return temp_file_id

    def add_manifest(self, root_uri: str, content: bytes) -> None:
        temp_file_id = self.temp_upload(content)
        response = _request(
            "POST",
            f"{self.base_url}/api/v1/resources",
            headers=self.headers,
            json_body={
                "temp_file_id": temp_file_id,
                "to": f"{root_uri}probe/manifest.md",
                "reason": "DocMind Train E version-root capability probe",
                "instruction": "",
                "wait": True,
                "timeout": 600,
                "strict": True,
                "source_name": "manifest.md",
                "telemetry": False,
                "watch_interval": 0,
                "processing_mode": "semantic_and_vectors",
            },
            timeout=660,
        )
        self._result(response, "/api/v1/resources")

    def tree(self, root_uri: str) -> list[dict[str, Any]]:
        result = self.get(
            "/api/v1/fs/tree",
            uri=root_uri,
            output="original",
            show_all_hidden="true",
            node_limit=1000,
            level_limit=10,
        )
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise CapabilityError("OpenViking tree is malformed")
        return result

    def content(self, endpoint: str, uri: str) -> str:
        result = self.get(endpoint, uri=uri)
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("content", "text", "data"):
                if isinstance(result.get(key), str):
                    return result[key]
        raise CapabilityError(f"{endpoint} returned no text")

    def no_watch(self, target_uri: str) -> bool:
        response = _request(
            "GET",
            f"{self.base_url}/api/v1/watches",
            headers=self.headers,
            params={"to_uri": target_uri},
            timeout=30,
        )
        self.calls.append("/api/v1/watches")
        if response.status_code == 404:
            return True
        if response.status_code >= 400:
            raise CapabilityError(f"watch lookup failed with HTTP {response.status_code}")
        payload = response.json()
        return payload.get("status") == "ok" and payload.get("result") is None

    def running_tasks(self) -> list[dict[str, Any]]:
        result = self.get("/api/v1/tasks", limit=200)
        if not isinstance(result, list):
            raise CapabilityError("task list is malformed")
        return [
            task
            for task in result
            if task.get("status") in {"pending", "running", "cancelling"}
        ]


def root_identity(client: OpenVikingClient, root_uri: str, source_sha256: str) -> dict[str, Any]:
    tree = client.tree(root_uri)
    by_suffix: dict[str, str] = {}
    for row in tree:
        uri = row.get("uri")
        if not isinstance(uri, str) or not uri.startswith(root_uri):
            raise CapabilityError("tree contains an out-of-root URI")
        suffix = uri[len(root_uri) :].rstrip("/")
        if not suffix or suffix in by_suffix:
            raise CapabilityError("tree contains an empty or duplicate suffix")
        by_suffix[suffix] = uri
    if set(by_suffix) != EXPECTED_ENTRY_SUFFIXES:
        raise CapabilityError("probe root layout differs from the exact nine-entry contract")
    l2 = client.content("/api/v1/content/read", by_suffix["probe/manifest.md/manifest.md"])
    abstract = client.content("/api/v1/content/abstract", by_suffix["probe/.abstract.md"])
    overview = client.content("/api/v1/content/overview", by_suffix["probe/.overview.md"])
    if _sha_bytes(l2.encode()) != source_sha256:
        raise CapabilityError("probe L2 source bytes changed")
    if not abstract.strip() or not overview.strip():
        raise CapabilityError("probe L0/L1 sidecar is empty")
    target_uri = f"{root_uri}probe/manifest.md"
    if not client.no_watch(target_uri):
        raise CapabilityError("watch_interval=0 created a watch")
    identity = {
        "root_uri": root_uri,
        "entry_count": len(by_suffix),
        "inventory_sha256": _sha_json(sorted(by_suffix)),
        "source_sha256": source_sha256,
        "l0_sha256": _sha_bytes(abstract.encode()),
        "l1_sha256": _sha_bytes(overview.encode()),
        "watch_interval": 0,
    }
    identity["identity_sha256"] = _sha_json(identity)
    return identity


def wait_root_identity(
    client: OpenVikingClient,
    root_uri: str,
    source_sha256: str,
    *,
    timeout_seconds: int = 180,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: CapabilityError | None = None
    while time.monotonic() < deadline:
        try:
            return root_identity(client, root_uri, source_sha256)
        except CapabilityError as error:
            if not any(
                marker in str(error)
                for marker in (
                    "nine-entry contract",
                    "L0/L1 sidecar is empty",
                )
            ):
                raise
            last_error = error
            sleep(2)
    raise CapabilityError("probe root did not finish exact L0/L1 materialization") from last_error


def _docker_identity(container: str) -> dict[str, str]:
    container_id = subprocess.check_output(
        ["docker", "inspect", container, "--format", "{{.Id}}"],
        text=True,
    ).strip()
    image_id = subprocess.check_output(
        ["docker", "inspect", container, "--format", "{{.Image}}"],
        text=True,
    ).strip()
    if not container_id or not image_id:
        raise CapabilityError("OpenViking Docker identity is unavailable")
    return {"container_id": container_id, "image_id": image_id}


def wait_ready(client: OpenVikingClient, timeout_seconds: int = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = client.get("/api/v1/system/status")
            if isinstance(result, dict) and result.get("initialized") is True:
                return
        except Exception as error:
            last_error = error
        time.sleep(2)
    raise CapabilityError("OpenViking did not become ready after restart") from last_error


def run_gate(
    client: OpenVikingClient,
    *,
    restart: Callable[[], None],
    docker_identity: Callable[[], dict[str, str]],
    nonce: str,
) -> dict[str, Any]:
    if client.running_tasks():
        raise CapabilityError("OpenViking has active tasks before capability gate")
    first_root = f"viking://resources/docmind-capability-{nonce}-a/"
    second_root = f"viking://resources/docmind-capability-{nonce}-b/"
    first_source = (
        "# DocMind Capability A\n\nResponsibility: version-root persistence probe A.\n"
        "Boundary: must remain distinct from probe B and every serving root.\n"
    ).encode()
    second_source = (
        "# DocMind Capability B\n\nResponsibility: version-root persistence probe B.\n"
        "Boundary: must not mutate probe A or any serving root.\n"
    ).encode()
    before_runtime = docker_identity()
    client.add_manifest(first_root, first_source)
    first_before_restart = wait_root_identity(client, first_root, _sha_bytes(first_source))
    if client.running_tasks():
        raise CapabilityError("OpenViking still has active tasks after first probe")
    restart()
    wait_ready(client)
    after_restart_runtime = docker_identity()
    if after_restart_runtime["image_id"] != before_runtime["image_id"]:
        raise CapabilityError("OpenViking image changed during restart")
    first_after_restart = wait_root_identity(client, first_root, _sha_bytes(first_source))
    if first_after_restart != first_before_restart:
        raise CapabilityError("first probe root changed across restart")
    client.add_manifest(second_root, second_source)
    second_identity = wait_root_identity(client, second_root, _sha_bytes(second_source))
    first_after_second = wait_root_identity(client, first_root, _sha_bytes(first_source))
    if first_after_second != first_before_restart:
        raise CapabilityError("older root changed after newer root creation")
    simulated_pointer = second_root
    first_after_pointer = wait_root_identity(client, first_root, _sha_bytes(first_source))
    if simulated_pointer != second_root or first_after_pointer != first_before_restart:
        raise CapabilityError("older root changed after simulated pointer publication")
    missing_root = f"viking://resources/docmind-capability-{nonce}-missing/"
    try:
        missing_tree = client.tree(missing_root)
    except CapabilityError:
        missing_detected = True
    else:
        missing_detected = len(missing_tree) == 0
    if not missing_detected:
        raise CapabilityError("missing root was not detectably unavailable")
    return {
        "schema": "docmind-train-e-openviking-capability-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS",
        "processing_contract": {
            "strict": True,
            "wait": True,
            "processing_mode": "semantic_and_vectors",
            "telemetry": False,
            "watch_interval": 0,
            "source_name": "manifest.md",
        },
        "runtime_before": before_runtime,
        "runtime_after_restart": after_restart_runtime,
        "first_before_restart": first_before_restart,
        "first_after_restart": first_after_restart,
        "second_root": second_identity,
        "older_root_after_newer_root_sha256": first_after_second["identity_sha256"],
        "older_root_after_simulated_pointer_sha256": first_after_pointer["identity_sha256"],
        "missing_root_detected": missing_detected,
        "active_catalog_or_serving_root_changed": False,
        "call_audit": list(client.calls),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--container", default=CONTAINER)
    parser.add_argument("--api-key-file", type=Path)
    args = parser.parse_args()
    key = os.environ.get("OPENVIKING_DATA_API_KEY", "").strip()
    if not key and args.api_key_file:
        key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not key:
        raise CapabilityError(
            "set OPENVIKING_DATA_API_KEY or pass --api-key-file"
        )
    client = OpenVikingClient(args.base_url, key)
    nonce = f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid4().hex[:8]}"
    result = run_gate(
        client,
        restart=lambda: subprocess.run(
            ["docker", "restart", args.container],
            check=True,
            stdout=subprocess.DEVNULL,
        ),
        docker_identity=lambda: _docker_identity(args.container),
        nonce=nonce,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(json.dumps({"status": result["status"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
