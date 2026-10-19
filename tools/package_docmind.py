"""Build and verify a DOCMIND source deployment ZIP from committed blobs only."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import posixpath
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "docmind-manifest.json"
FORBIDDEN_PARTS = {".git", ".local", ".venv", "node_modules", "__pycache__", "runtime", "uploads", "logs", "dist"}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".db", ".pyc", ".gguf", ".safetensors", ".ckpt", ".onnx", ".pdf", ".hwp", ".hwpx", ".enc"}


def safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name) and not path.is_absolute() and "\\" not in name
        and not any(part in {"", ".", ".."} or ":" in part for part in name.split("/"))
        and not any(part.lower() in FORBIDDEN_PARTS or "private" in part.lower() for part in path.parts)
        and path.suffix.lower() not in FORBIDDEN_SUFFIXES
        and not any(part.lower().startswith(".env") and not part.endswith(".example") for part in path.parts)
        and name != "app/conf/service_conf.yaml"
    )


def selected(name: str, spec: dict) -> bool:
    return safe_path(name) and (
        name in spec["files"] or any(name.startswith(tree + "/") for tree in spec["trees"])
    ) and not any(fnmatch.fnmatchcase(name, pattern) for pattern in spec["exclude"])


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def verify(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or MANIFEST not in names:
            raise ValueError("Duplicate ZIP entries or missing manifest")
        manifest = json.loads(archive.read(MANIFEST))
        if manifest.get("schema") != "docmind-package-v1" or not re.fullmatch(r"[0-9a-f]{40,64}", manifest.get("source_commit", "")):
            raise ValueError("Invalid package manifest")
        expected = {row["path"]: row for row in manifest["files"]}
        if len(expected) != len(manifest["files"]) or set(names) != set(expected) | {MANIFEST}:
            raise ValueError("Package contents differ from manifest")
        for name, row in expected.items():
            if not safe_path(name):
                raise ValueError("Unsafe or private package path")
            content = archive.read(name)
            if len(content) != row["bytes"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
                raise ValueError("Package content hash mismatch")
        return {"verified": True, "files": len(expected), "source_commit": manifest["source_commit"]}


def build(output: Path) -> dict:
    if output.exists():
        raise ValueError("Output exists; choose a fresh path")
    commit = git("rev-parse", "HEAD").decode().strip()
    spec = json.loads(git("show", commit + ":deploy/package.json"))
    tracked = set(git("ls-tree", "-r", "--name-only", commit).decode().splitlines())
    paths = {name for name in tracked if selected(name, spec)}
    # Include only installation documents linked by the selected entry guides.
    pending = [name for name in paths if name.endswith(".md")]
    visited = set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        text = git("show", commit + ":" + name).decode("utf-8-sig")
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if ":" in link or link.startswith("#"):
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(link.split("#")[0])))
            if target in tracked and safe_path(target) and target.endswith(".md") and target not in paths:
                paths.add(target)
                pending.append(target)
    if not set(spec["required"]).issubset(paths):
        raise ValueError("Required DOCMIND package input missing")
    output.parent.mkdir(parents=True, exist_ok=True)
    export_roots = set(spec["trees"]) | set(spec["files"]) | {name for name in paths if name.endswith(".md")}
    process = subprocess.Popen(["git", "archive", "--format=tar", commit, "--", *sorted(export_roots)], cwd=ROOT, stdout=subprocess.PIPE)
    rows = []
    try:
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            with tarfile.open(fileobj=process.stdout, mode="r|") as source:
                for member in source:
                    if member.isdir() or member.name not in paths:
                        continue
                    if not member.isfile():
                        raise ValueError("Unexpected archive entry or symbolic link")
                    stream = source.extractfile(member)
                    digest = hashlib.sha256()
                    info = zipfile.ZipInfo(member.name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (0o100000 | member.mode) << 16
                    with archive.open(info, "w") as dest:
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                            dest.write(chunk)
                    rows.append({"path": member.name, "bytes": member.size, "sha256": digest.hexdigest()})
            if process.wait() != 0 or {row["path"] for row in rows} != paths:
                raise ValueError("Git export failed or input was excluded by Git attributes")
            archive.writestr(MANIFEST, json.dumps({"schema": spec["schema"], "source_commit": commit, "files": rows}, indent=2) + "\n")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        if process.stdout:
            process.stdout.close()
    return verify(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--verify", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.verify) if args.verify else build(args.output)))


if __name__ == "__main__":
    main()
