#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "release/public-source-manifest.json"


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def main() -> int:
    changed = _git(
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "v0.27.1",
    ).splitlines()
    paths = sorted(path for path in changed if path != OUTPUT.relative_to(ROOT).as_posix())
    files = []
    for relative in paths:
        source = ROOT / relative
        if not source.is_file():
            continue
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
    payload = {
        "schema": "docmind-public-source-manifest-v1",
        "upstream": {
            "repository": "https://github.com/infiniflow/ragflow",
            "tag": "v0.27.1",
            "commit": _git("rev-parse", "v0.27.1"),
        },
        "files": files,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)} with {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
