"""One RHWP document parse and one page render per isolated child process."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 6:
        return 2
    source, source_format, page_text, raw_path, metadata_path = sys.argv[1:]
    if source_format not in {"hwp", "hwpx"}:
        return 2
    page = int(page_text)
    from rhwp import Document

    document = Document.from_bytes(Path(source).read_bytes(), source_uri=f"memory://source.{source_format}")
    try:
        count = int(document.page_count)
        if page < 1 or page > count:
            return 3
        svg = document.render_svg(page - 1)
        encoded = svg.encode("utf-8")
        if len(encoded) > 16 * 1024 * 1024:
            return 4
        Path(raw_path).write_bytes(encoded)
        Path(metadata_path).write_text(json.dumps({"page_count": count}), encoding="utf-8")
        return 0
    finally:
        close = getattr(document, "close", None) or getattr(document, "unload", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
