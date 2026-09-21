"""Capture an existing local service configuration without printing its secrets.

Generates a private Compose override for a frontend-only image replacement.
This does not replace, stop, or start any container.
"""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def docker_json(*args):
    return json.loads(subprocess.check_output(["docker", *args], text=True))


def private_file(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def normalized_mount_source(mount_type, source):
    if mount_type == "bind" and source.startswith("/host_mnt/Users/"):
        return source.removeprefix("/host_mnt")
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="docker-ragflow-1")
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    current = docker_json("inspect", args.container)[0]
    if not current["State"]["Running"]:
        raise RuntimeError("The existing service must be running before replacement")
    config = current["Config"]
    labels = config["Labels"]
    compose_files = labels["com.docker.compose.project.config_files"].split(",")
    project = labels["com.docker.compose.project"]
    service = labels["com.docker.compose.service"]
    env = dict(item.split("=", 1) for item in config["Env"])
    output = Path(args.output).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    private_file(output / "before.inspect.json", json.dumps(current, indent=2))
    for filename, image in (("release.yml", args.image), ("rollback.yml", current["Image"])):
        override = "services:\n  " + service + ":\n    image: " + json.dumps(image) + "\n    env_file: !reset []\n    environment: !override " + json.dumps(env, ensure_ascii=False) + "\n"
        private_file(output / filename, override)
    command = ["docker", "compose", "-p", project]
    for path in compose_files:
        command.extend(["-f", path])
    command.extend(["-f", str(output / "release.yml")])
    resolved = json.loads(subprocess.check_output([*command, "config", "--format", "json"], text=True))
    candidate = resolved["services"][service]
    if candidate["environment"] != env:
        raise RuntimeError("Resolved environment does not match the live service")
    # Preserve the existing document/artifact/log mounts exactly.
    mounts = set()
    for mount in candidate.get("volumes", []):
        source = mount["source"]
        if mount["type"] == "volume":
            source = resolved["volumes"][source].get("name", source)
        source = normalized_mount_source(mount["type"], source)
        mounts.add((mount["type"], source, mount["target"], not mount.get("read_only", False)))
    live_mounts = {
        (
            m["Type"],
            normalized_mount_source(
                m["Type"],
                m.get("Name") if m["Type"] == "volume" else m["Source"],
            ),
            m["Destination"],
            m["RW"],
        )
        for m in current["Mounts"]
    }
    if mounts != live_mounts:
        raise RuntimeError("Resolved mounts do not match the live service")
    private_file(output / "resolved.compose.json", json.dumps(resolved, indent=2))
    report = {
        "container": args.container,
        "before_image": current["Image"],
        "release_image": args.image,
        "project": project,
        "service": service,
        "environment_matches": True,
        "environment_sha256": hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest(),
        "mounts_match": True,
        "compose_files": compose_files,
        "release_override": str(output / "release.yml"),
        "rollback_override": str(output / "rollback.yml"),
    }
    private_file(output / "preflight.json", json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
