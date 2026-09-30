"""The PPTX text fallback reads only bounded, ordered slide XML."""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from rag.parser_platform.config import ParserPlatformConfig
from rag.parser_platform.coordinator import PreparedParserRun
from rag.parser_platform.dispatch import ParserSelection
from rag.parser_platform.kordoc_pilot import KordocServiceError
from rag.parser_platform.kordoc_pptx_text_fallback import pptx_text_result
from rag.parser_platform.schemas import SourceFormat
from rag.parser_platform.standard_bridge import ParserPlatformStandardBridge


def pptx_archive(*, slides: dict[str, str], order: tuple[str, ...]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        ids = "".join(f'<p:sldId r:id="rId{slide}"/>' for slide in order)
        archive.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst>' + ids + '</p:sldIdLst></p:presentation>')
        links = "".join(f'<Relationship Id="rId{slide}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{slide}.xml"/>' for slide in order)
        archive.writestr("ppt/_rels/presentation.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + links + '</Relationships>')
        for slide, content in slides.items():
            archive.writestr(f"ppt/slides/slide{slide}.xml", content)
    return payload.getvalue()


class PptxTextFallbackTest(unittest.TestCase):
    def test_preserves_presentation_order_and_marks_partial_content(self):
        paragraph = lambda value: '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:p><a:r><a:t>' + value + '</a:t></a:r></a:p></p:sld>'
        source = pptx_archive(slides={"1": paragraph("One"), "2": paragraph("Two")}, order=("2", "1"))
        result = pptx_text_result(source, parser_version="4.15.7", patch_revision="sha256:test")
        self.assertEqual([block["text"] for block in result["blocks"]], ["Two", "One"])
        self.assertEqual([block["pageNumber"] for block in result["blocks"]], [1, 2])
        self.assertEqual(result["source_hash"], hashlib.sha256(source).hexdigest())
        self.assertIn({"code": "PPTX_XML_TEXT_FALLBACK"}, result["warnings"])

    def test_refuses_xml_entities_and_empty_slide_text(self):
        malicious = '<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><p:sld>&secret;</p:sld>'
        source = pptx_archive(slides={"1": malicious}, order=("1",))
        with self.assertRaises(ValueError):
            pptx_text_result(source, parser_version="4.15.7", patch_revision="sha256:test")
        empty = pptx_archive(slides={"1": "<p:sld/>"}, order=("1",))
        with self.assertRaises(ValueError):
            pptx_text_result(empty, parser_version="4.15.7", patch_revision="sha256:test")

    def test_refuses_oversized_slide_xml(self):
        source = pptx_archive(slides={"1": "x" * (16 * 1024 * 1024 + 1)}, order=("1",))
        with self.assertRaises(ValueError):
            pptx_text_result(source, parser_version="4.15.7", patch_revision="sha256:test")

    def test_bridge_uses_fallback_only_when_enabled(self):
        fixture = Path(__file__).resolve().parents[4] / "parser_services/kordoc/test/fixtures/three-slides.pptx"
        source = fixture.read_bytes()
        prepared = PreparedParserRun(
            parse_run_id="a" * 32, chunk_set_id="b" * 32,
            idempotency_key="i" * 64, source_fingerprint="s" * 64,
            config_fingerprint="c" * 64, parser_fingerprint="f" * 64,
            selection=ParserSelection(SourceFormat.PPTX, "kordoc", "office_document_parse", "kordoc_pptx_global"),
            parser_name="kordoc", parser_version="4.15.7", model_version=None,
            backend="kordoc-offline", attempt=0,
        )

        def conversion_failed(*_args, **_kwargs):
            raise KordocServiceError("PARSER_INVALID_INPUT")

        with tempfile.TemporaryDirectory() as root:
            with patch("rag.parser_platform.kordoc_pilot.parse_pilot_service", side_effect=conversion_failed):
                enabled = ParserPlatformStandardBridge.from_config(ParserPlatformConfig(
                    artifact_root=root, pptx_text_fallback_enabled=True,
                ))
                document = enabled.parse(
                    prepared=prepared, source_bytes=source, source_document_id="doc-a",
                    source_format=SourceFormat.PPTX, trace_id="trace-a",
                )
                self.assertTrue(document.blocks)
                self.assertIn("PPTX_XML_TEXT_FALLBACK", document.warnings)
                self.assertEqual(document.diagnostics["conversion"], "pptx-xml-text")
                disabled = ParserPlatformStandardBridge.from_config(ParserPlatformConfig(artifact_root=root))
                with self.assertRaisesRegex(Exception, "kordoc service failed"):
                    disabled.parse(
                        prepared=prepared, source_bytes=source, source_document_id="doc-a",
                        source_format=SourceFormat.PPTX, trace_id="trace-a",
                    )
            with patch("rag.parser_platform.kordoc_pilot.parse_pilot_service",
                       side_effect=KordocServiceError("PARSER_BUSY")):
                with self.assertRaisesRegex(Exception, "kordoc service failed"):
                    enabled.parse(
                        prepared=prepared, source_bytes=source, source_document_id="doc-a",
                        source_format=SourceFormat.PPTX, trace_id="trace-a",
                    )

    def test_flag_changes_only_pptx_run_fingerprint(self):
        disabled = ParserPlatformConfig()
        enabled = ParserPlatformConfig(pptx_text_fallback_enabled=True)
        self.assertNotEqual(disabled.run_config_fingerprint("pptx"), enabled.run_config_fingerprint("pptx"))
        self.assertEqual(disabled.run_config_fingerprint("pdf"), enabled.run_config_fingerprint("pdf"))
