from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import processor


def package(parts: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in parts.items():
            archive.writestr(name, value)
    return output.getvalue()


class ProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.session = self.root / "session-1"
        self.session.mkdir()
        self.output = self.session / "output"
        self.output.mkdir()
        self.root_patch = patch.object(processor, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_direct_formats_return_original_without_conversion(self) -> None:
        source = self.session / "source.pdf"
        source.write_bytes(b"%PDF-1.7\nsynthetic")
        with patch.object(processor, "convert_office", side_effect=AssertionError("converted")):
            result = processor.process("session-1", str(source), "pdf", str(self.output))
        self.assertEqual(result["content_path"], str(source))
        self.assertEqual(result["viewer_kind"], "pdf")

    def test_package_must_have_expected_structure(self) -> None:
        source = self.session / "source.pptx"
        source.write_bytes(package({"[Content_Types].xml": b"x", "ppt/presentation.xml": b"y"}))
        self.assertEqual(processor.process("session-1", str(source), "pptx", str(self.output))["viewer_kind"], "powerpoint")
        source.write_bytes(package({"[Content_Types].xml": b"x", "word/document.xml": b"y"}))
        with self.assertRaisesRegex(processor.PreviewError, "INVALID_PREVIEW_PACKAGE"):
            processor.process("session-1", str(source), "pptx", str(self.output))

    def test_path_escape_and_symlink_are_rejected(self) -> None:
        source = self.session / "source.pdf"
        source.write_bytes(b"%PDF-1.7\nsynthetic")
        with self.assertRaisesRegex(processor.PreviewError, "UNSAFE_PREVIEW_PATH"):
            processor.process("session-1", str(source), "pdf", str(self.root))
        link = self.session / "link.pdf"
        try:
            link.symlink_to(source)
        except (OSError, NotImplementedError):
            return
        with self.assertRaisesRegex(processor.PreviewError, "UNSAFE_PREVIEW_PATH"):
            processor.process("session-1", str(link), "pdf", str(self.output))

    def test_svg_strips_script_external_refs_and_events(self) -> None:
        svg = b"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100"><script>alert(1)</script><image xlink:href="https://example.invalid/pixel.png"/><rect onclick="alert(1)" fill="url(https://example.invalid/fill)" width="20" height="20"/><text>Korean</text></svg>"""
        cleaned = processor.sanitize_svg(svg).decode("utf-8")
        self.assertNotIn("script", cleaned)
        self.assertNotIn("example.invalid", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertIn("Korean", cleaned)

    def test_cancel_sets_active_operation(self) -> None:
        event = processor.begin_operation("session-1")
        try:
            self.assertEqual(processor.cancel("session-1"), {"cancelled": True})
            self.assertTrue(event.is_set())
        finally:
            processor.end_operation("session-1")

    @unittest.skipUnless(hasattr(os, "killpg"), "POSIX process group required")
    def test_cancel_kills_child_process(self) -> None:
        event = processor.begin_operation("session-1")
        errors: list[Exception] = []

        def run() -> None:
            try:
                processor._run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=30, session_id="session-1", cancel_event=event)
            except processor.PreviewError as error:
                errors.append(error)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            for _ in range(100):
                with processor._ACTIVE_LOCK:
                    active = processor._ACTIVE.get("session-1")
                    if active and active[1] is not None:
                        break
                time.sleep(0.01)
            self.assertEqual(processor.cancel("session-1"), {"cancelled": True})
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(str(errors[0]), "PREVIEW_CANCELLED")
            with processor._ACTIVE_LOCK:
                self.assertIsNone(processor._ACTIVE["session-1"][1])
        finally:
            processor.end_operation("session-1")

    def test_page_render_failure_removes_raw_file(self) -> None:
        source = self.session / "source.hwp"
        source.write_bytes(processor.OLE_MAGIC + b"synthetic")
        raw = self.output / "page-1.raw.svg"

        def fail(*args: object, **kwargs: object) -> None:
            raw.write_bytes(b"partial")
            raise processor.PreviewError("PREVIEW_CANCELLED")

        with patch.object(processor, "_run", side_effect=fail), self.assertRaisesRegex(processor.PreviewError, "PREVIEW_CANCELLED"):
            processor.render_hwp_page(source, self.output, "hwp", 1, "session-1", threading.Event())
        self.assertFalse(raw.exists())


if __name__ == "__main__":
    unittest.main()
