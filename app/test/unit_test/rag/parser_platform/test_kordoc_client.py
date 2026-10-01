"""HTTP client carries the per-job PDF safety limit to Kordoc."""

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from rag.parser_platform.kordoc_pilot import KordocPageLimitExceeded, KordocServiceError, parse_pilot_service


class KordocClientTest(unittest.TestCase):
    def test_page_cap_is_sent_and_reported_with_observed_count(self):
        seen = {}

        def fail_with_page_count(request, *, timeout):
            seen.update(json.loads(request.data))
            body = io.BytesIO(json.dumps({
                "code": "PARSER_PAGE_LIMIT_EXCEEDED", "page_count": 31,
            }).encode())
            raise HTTPError(request.full_url, 400, "page cap", {}, body)

        with patch("rag.parser_platform.kordoc_pilot.urllib.request.urlopen", side_effect=fail_with_page_count):
            with self.assertRaises(KordocPageLimitExceeded) as caught:
                parse_pilot_service(
                    b"%PDF-1.7\nsynthetic", "pdf",
                    service_url="http://kordoc-parser:8095", max_pdf_pages=30,
                )
        self.assertEqual(seen["max_pdf_pages"], 30)
        self.assertEqual(caught.exception.page_count, 31)

    def test_invalid_input_code_is_preserved_without_service_details(self):
        def fail(request, *, timeout):
            body = io.BytesIO(b'{"code":"PARSER_INVALID_INPUT","detail":"private"}')
            raise HTTPError(request.full_url, 400, "invalid", {}, body)

        with patch("rag.parser_platform.kordoc_pilot.urllib.request.urlopen", side_effect=fail):
            with self.assertRaises(KordocServiceError) as caught:
                parse_pilot_service(b"synthetic", "pptx", service_url="http://kordoc-parser:8095")
        self.assertEqual(caught.exception.code, "PARSER_INVALID_INPUT")
        self.assertNotIn("private", str(caught.exception))

    def test_pdf_ocr_flag_is_never_sent_to_kordoc(self):
        seen = []

        def fail(request, *, timeout):
            seen.append(json.loads(request.data))
            raise HTTPError(request.full_url, 400, "invalid", {}, io.BytesIO(b'{}'))

        with patch("rag.parser_platform.kordoc_pilot.urllib.request.urlopen", side_effect=fail):
            with self.assertRaises(KordocServiceError):
                parse_pilot_service(b"synthetic", "pdf", service_url="http://kordoc-parser:8095")
        self.assertNotIn("pdf_ocr_requested", seen[0])
        with self.assertRaises(ValueError):
            parse_pilot_service(b"synthetic", "pdf", service_url="http://kordoc-parser:8095",
                                pdf_ocr_requested=True)
