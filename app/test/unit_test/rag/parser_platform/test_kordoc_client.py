"""HTTP client carries the per-job PDF safety limit to Kordoc."""

import io
import hashlib
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch
from urllib.error import HTTPError

from rag.parser_platform.kordoc_pilot import KordocPageLimitExceeded, KordocServiceError, parse_pilot_service


class KordocClientTest(unittest.TestCase):
    def test_slow_drip_success_and_error_bodies_obey_wall_deadline(self):
        for status in [200, 503]:
            with self.subTest(status=status):
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass
                    def do_POST(self):
                        self.rfile.read(int(self.headers['Content-Length']))
                        self.send_response(status)
                        self.send_header('Content-Length', '100')
                        self.end_headers()
                        try:
                            for _ in range(100):
                                self.wfile.write(b' ')
                                self.wfile.flush()
                                time.sleep(.02)
                        except OSError:
                            pass
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    started = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        parse_pilot_service(b'synthetic', 'hwp', service_url=f'http://127.0.0.1:{server.server_port}', timeout=.12)
                    self.assertLess(time.monotonic() - started, .4)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()

    def test_busy_until_deadline_records_wait_and_never_retries_cleanup_failure(self):
        stages = []
        @contextmanager
        def timing(name):
            stages.append(name)
            yield
        def busy(request, *, timeout):
            raise HTTPError(request.full_url, 503, 'busy', {}, io.BytesIO(b'{"code":"PARSER_BUSY"}'))
        with patch('rag.parser_platform.kordoc_pilot.urllib.request.urlopen', side_effect=busy):
            with self.assertRaises(TimeoutError):
                parse_pilot_service(b'synthetic', 'hwp', service_url='http://always-busy', timeout=.08, timing_scope=timing)
        self.assertEqual(stages[:2], ['parser_queue', 'parser_serialize'])
        self.assertIn('parser_transport', stages)
        self.assertIn('parser_busy_wait', stages)
        error = HTTPError('http://failed', 503, 'failed', {}, io.BytesIO(b'{"code":"PARSER_CLEANUP_FAILED"}'))
        with patch('rag.parser_platform.kordoc_pilot.urllib.request.urlopen', side_effect=error) as send:
            with self.assertRaises(KordocServiceError) as caught:
                parse_pilot_service(b'synthetic', 'hwp', service_url='http://failed', timeout=.08)
        self.assertEqual(caught.exception.code, 'PARSER_CLEANUP_FAILED')
        self.assertEqual(send.call_count, 1)

    def test_pool_capacity_admits_workers_and_does_not_serialize_queued_source(self):
        for capacity in (2, 4):
            with self.subTest(capacity=capacity):
                self._check_pool_capacity(capacity)

    def _check_pool_capacity(self, capacity):
        entered = threading.Barrier(capacity + 1)
        release = threading.Event()
        encoded = []
        original_encode = __import__('base64').b64encode
        def encode(value):
            encoded.append(value)
            return original_encode(value)
        def respond(request, *, timeout):
            entered.wait(timeout=2)
            self.assertTrue(release.wait(3))
            return io.BytesIO(json.dumps({'source_hash': hashlib.sha256(b'synthetic').hexdigest(),
                                         'source_format': 'hwp', 'parser_name': 'kordoc'}).encode())
        service_url = f'http://pool-parser-{capacity}:8095'
        with patch.dict(os.environ, {'KORDOC_CLIENT_CONCURRENCY': str(capacity)}), \
             patch('rag.parser_platform.kordoc_pilot.urllib.request.urlopen', side_effect=respond), \
             patch('rag.parser_platform.kordoc_pilot.base64.b64encode', side_effect=encode):
            with ThreadPoolExecutor(capacity) as pool:
                futures = [pool.submit(parse_pilot_service, b'synthetic', 'hwp',
                           service_url=service_url, timeout=2) for _ in range(capacity)]
                try:
                    entered.wait(timeout=2)
                    with self.assertRaises(TimeoutError):
                        parse_pilot_service(b'queued', 'hwp', service_url=service_url, timeout=.05)
                    self.assertEqual(encoded, [b'synthetic'] * capacity)
                finally:
                    release.set()
                self.assertTrue(all(f.result()['parser_name'] == 'kordoc' for f in futures))

    def test_busy_server_retries_within_original_deadline(self):
        success = json.dumps({'source_hash': hashlib.sha256(b'synthetic').hexdigest(),
                              'source_format': 'hwp', 'parser_name': 'kordoc'}).encode()
        timeouts = []
        def respond(request, *, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise HTTPError(request.full_url, 503, 'busy', {}, io.BytesIO(b'{"code":"PARSER_BUSY"}'))
            return io.BytesIO(success)
        with patch('rag.parser_platform.kordoc_pilot.urllib.request.urlopen', side_effect=respond):
            result = parse_pilot_service(b'synthetic', 'hwp', service_url='http://busy-parser:8095', timeout=1)
        self.assertEqual(result['parser_name'], 'kordoc')
        self.assertEqual(len(timeouts), 2)
        self.assertLess(timeouts[1], timeouts[0])

    def test_same_parser_waits_until_previous_response_is_read(self):
        entered = threading.Event()
        second_started = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()
        calls = []

        def respond(request, *, timeout):
            calls.append(request)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(3))
            else:
                second_entered.set()
            return io.BytesIO(json.dumps({"source_hash": hashlib.sha256(b"synthetic").hexdigest(),
                                         "source_format": "hwp", "parser_name": "kordoc"}).encode())

        def call(second=False):
            if second:
                second_started.set()
            return parse_pilot_service(b"synthetic", "hwp", service_url="http://serial-parser:8095", timeout=2)

        with patch("rag.parser_platform.kordoc_pilot.urllib.request.urlopen", side_effect=respond):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(call)
                self.assertTrue(entered.wait(2))
                second = pool.submit(call, True)
                self.assertTrue(second_started.wait(2))
                try:
                    self.assertFalse(second_entered.wait(.15), "single-job parser received concurrent requests")
                finally:
                    release.set()
                self.assertEqual(first.result()["source_format"], "hwp")
                self.assertEqual(second.result()["source_format"], "hwp")
        self.assertEqual(len(calls), 2)

    def test_queue_wait_uses_request_deadline_and_does_not_send_expired_job(self):
        entered = threading.Event()
        release = threading.Event()

        def respond(request, *, timeout):
            entered.set()
            self.assertTrue(release.wait(3))
            return io.BytesIO(json.dumps({"source_hash": hashlib.sha256(b"synthetic").hexdigest(),
                                         "source_format": "hwp", "parser_name": "kordoc"}).encode())

        with patch("rag.parser_platform.kordoc_pilot.urllib.request.urlopen", side_effect=respond) as send:
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(parse_pilot_service, b"synthetic", "hwp", service_url="http://deadline-parser:8095", timeout=2)
                self.assertTrue(entered.wait(2))
                try:
                    with self.assertRaises(TimeoutError):
                        parse_pilot_service(b"synthetic", "hwp", service_url="http://deadline-parser:8095", timeout=.05)
                finally:
                    release.set()
                first.result()
        self.assertEqual(send.call_count, 1)

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
