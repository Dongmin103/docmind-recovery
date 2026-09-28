"""Manual synthetic smoke inside the resource-limited preview container.

Mount generated fixtures read-only at /fixtures and a 256 MiB tmpfs at
/run/docmind-previews. Prints metadata/timing only; never source text.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/opt/docmind-preview")
import processor

ROOT = Path("/run/docmind-previews")


def memory_bytes():
    return int(Path("/sys/fs/cgroup/memory.current").read_text())


def make_conversion(source, destination, filter_name):
    profile = destination / "fixture-profile"
    subprocess.run(
        ["/usr/bin/soffice", "--headless", "--nologo", "--nodefault", "--nofirststartwizard",
         f"-env:UserInstallation={profile.as_uri()}", "--convert-to", filter_name,
         "--outdir", str(destination), str(source)],
        check=True, timeout=60, stdin=subprocess.DEVNULL,
        env={**os.environ, "HOME": str(destination), "TMPDIR": str(destination)},
    )
    if profile.exists():
        shutil.rmtree(profile)


def main():
    (ROOT / "processor-tmp").mkdir(mode=0o700, exist_ok=True)
    fixtures = ROOT / "fixture-generation"
    fixtures.mkdir(mode=0o700)
    for source in Path("/fixtures").glob("synthetic.*"):
        shutil.copyfile(source, fixtures / source.name)
    make_conversion(fixtures / "synthetic.docx", fixtures, "doc:MS Word 97")
    make_conversion(fixtures / "synthetic.pptx", fixtures, "ppt:MS PowerPoint 97")
    # PDF is a synthetic test input only; product previews never take this path.
    make_conversion(fixtures / "synthetic.docx", fixtures, "pdf:writer_pdf_Export")
    results = []
    try:
        for kind in ("pdf", "docx", "pptx", "xlsx", "xls", "doc", "ppt"):
            session_id = f"synthetic-{kind}"
            directory = ROOT / session_id
            directory.mkdir(mode=0o700)
            source = directory / f"input.{kind}"
            shutil.copyfile(fixtures / f"synthetic.{kind}", source)
            peak = [memory_bytes()]
            stop = threading.Event()

            def monitor(stop=stop, peak=peak):
                while not stop.wait(0.02):
                    peak[0] = max(peak[0], memory_bytes())

            thread = threading.Thread(target=monitor, daemon=True)
            thread.start()
            started = time.perf_counter()
            try:
                result = processor.process(session_id, str(source), kind, str(directory))
                results.append({
                    "source_format": kind, "display_format": result["display_format"],
                    "input_bytes": source.stat().st_size,
                    "output_bytes": Path(result["content_path"]).stat().st_size,
                    "processing_ms": round((time.perf_counter() - started) * 1000, 2),
                    "container_peak_mib_sampled": round(peak[0] / (1024 * 1024), 2),
                })
            finally:
                stop.set()
                thread.join()
                shutil.rmtree(directory)
    finally:
        shutil.rmtree(fixtures)
    print(json.dumps({"samples": results, "remaining_session_entries": len([
        path for path in ROOT.iterdir() if path.name != "processor-tmp"
    ]), "remaining_temp_entries": len(list((ROOT / "processor-tmp").iterdir()))}, indent=2))


if __name__ == "__main__":
    main()
