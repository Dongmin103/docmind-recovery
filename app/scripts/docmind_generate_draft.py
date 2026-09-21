#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import traceback

from api.apps.services import docmind_generation_service


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--draft-id", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(
            docmind_generation_service.generate_and_validate_draft(
                args.tenant_id,
                args.draft_id,
            )
        )
    except Exception as error:
        code = (
            error.code
            if isinstance(error, docmind_generation_service.DocmindGenerationError)
            else "DOCMIND_GENERATION_INTERNAL_ERROR"
        )
        docmind_generation_service.mark_failed(args.draft_id, code)
        if code == "DOCMIND_GENERATION_INTERNAL_ERROR":
            frames = [
                {
                    "file": frame.filename,
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in traceback.extract_tb(error.__traceback__)
            ]
            print(json.dumps({"internal_frames": frames}, sort_keys=True))
        print(json.dumps({"status": "FAILED", "code": code}, sort_keys=True))
        return 1
    print(json.dumps({"status": "READY", "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
