#!/usr/bin/env python3
"""Fail when the retired catalog integration re-enters product surfaces."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# These are immutable recovery or evaluation records, not deployment inputs.
# Keep this list exact: directories and glob patterns are intentionally rejected.
ALLOWED_EVIDENCE_FILES = frozenset(
    {
        "HANDOFF-MANIFEST.json",
        "HANDOFF-VERIFICATION.json",
        "README.md",
        "RESTORE.md",
        "VERIFICATION.json",
        "checkpoint.json",
        "docs/HANDOFF-STATUS.md",
        "docs/RAGFLOW-TRIM-AUDIT.md",
        "restore_backup.py",
    }
)

# Large tokenizer assets are immutable third-party data and are not executable
# product configuration. All dependency lockfiles remain in the scan.
IMMUTABLE_TEXT_ASSETS = frozenset(
    {
        "app/rag/res/bge/tokenizer.json",
        "app/rag/res/deepdoc/tokenizer.json",
        "app/web/public/tokenizer.json",
    }
)

BINARY_SUFFIXES = frozenset(
    {
        ".7z",
        ".avi",
        ".bin",
        ".bmp",
        ".bz2",
        ".eot",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mp3",
        ".mp4",
        ".pdf",
        ".png",
        ".so",
        ".tar",
        ".tiff",
        ".ttf",
        ".webm",
        ".woff",
        ".woff2",
        ".xz",
        ".zip",
    }
)

RUNTIME_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cfg",
        ".conf",
        ".cpp",
        ".cs",
        ".css",
        ".env",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".lock",
        ".mjs",
        ".ps1",
        ".py",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".xml",
        ".yaml",
        ".yml",
    }
)

_REMOVED_NAMES = ("open" + "viking", "open" + "-viking", "open" + "_viking")
_REMOVED_ENV_PREFIXES = (("OPEN" + "VIKING") + "_", ("OPEN" + "_VIKING") + "_")
_REMOVED_URI = "viking" + "://"

GLOBAL_PATTERNS = (("retired URI scheme", re.compile(re.escape(_REMOVED_URI), re.IGNORECASE)),) + tuple(
    ("retired environment prefix", re.compile(re.escape(prefix), re.IGNORECASE)) for prefix in _REMOVED_ENV_PREFIXES
)
RUNTIME_PATTERNS = GLOBAL_PATTERNS + tuple(("retired integration name", re.compile(re.escape(name), re.IGNORECASE)) for name in _REMOVED_NAMES)


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str


def _repository_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return sorted(path for path in result.stdout.decode("utf-8").split("\0") if path)


def _is_runtime_surface(relative_path: str) -> bool:
    path = Path(relative_path)
    lower_parts = tuple(part.lower() for part in path.parts)
    name = path.name.lower()

    if path.suffix.lower() in RUNTIME_SUFFIXES and lower_parts[:1] in {("app",), ("tools",)}:
        return True
    if lower_parts[:2] in {("app", "docker"), ("app", "scripts")}:
        return True
    if name.startswith(("dockerfile", "containerfile")):
        return True
    return name in {
        "gemfile.lock",
        "go.mod",
        "go.sum",
        "package-lock.json",
        "package.json",
        "pipfile.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "requirements.txt",
        "uv.lock",
        "yarn.lock",
    }


def scan_paths(root: Path, relative_paths: Iterable[str]) -> list[Finding]:
    findings: list[Finding] = []
    for relative_path in sorted(set(relative_paths)):
        normalized = Path(relative_path).as_posix()
        if normalized in ALLOWED_EVIDENCE_FILES or normalized in IMMUTABLE_TEXT_ASSETS:
            continue

        path = root / Path(normalized)
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        runtime_surface = _is_runtime_surface(normalized)
        markdown = path.suffix.lower() in {".md", ".mdx"}
        in_fenced_block = False
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            fence_boundary = markdown and stripped.startswith(("```", "~~~"))
            patterns = RUNTIME_PATTERNS if runtime_surface or in_fenced_block else GLOBAL_PATTERNS
            for rule, pattern in patterns:
                if pattern.search(line):
                    findings.append(Finding(normalized, line_number, rule))
            if fence_boundary:
                in_fenced_block = not in_fenced_block
    return sorted(findings)


def scan_repository(root: Path = REPOSITORY_ROOT) -> list[Finding]:
    return scan_paths(root, _repository_files(root))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify that the retired catalog runtime is absent from product surfaces.")
    parser.add_argument(
        "--root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="repository root (defaults to the checkout containing this script)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = args.root.resolve()
    findings = scan_repository(root)
    if findings:
        print("Retired catalog runtime references found:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding.path}:{finding.line}: {finding.rule}", file=sys.stderr)
        return 1
    print("Verified: retired catalog runtime references are absent from product surfaces.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
