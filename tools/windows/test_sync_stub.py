"""Synthetic signed endpoint for the native Windows incremental smoke test."""

import argparse
import hashlib
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    secret = bytes(range(32))
    last_sequence = 0

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            nonlocal last_sequence
            body = self.rfile.read(int(self.headers["Content-Length"]))
            digest = hashlib.sha256(body).hexdigest()
            stamp = self.headers.get("X-DocMind-Timestamp", "")
            nonce = self.headers.get("X-DocMind-Nonce", "")
            expected = hmac.new(secret, f"POST\n{self.path}\n{stamp}\n{nonce}\n{digest}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, self.headers.get("X-DocMind-Signature", "")):
                self.send_error(401)
                return
            request = json.loads(body)
            if self.path.endswith("/session"):
                result = {"source_id": request["source_id"], "epoch": 1, "last_sequence": last_sequence,
                          "lease_expires_at": "2099-01-01T00:00:00Z"}
            else:
                assert request["sequence"] == last_sequence + 1
                last_sequence += 1
                result = {"accepted": True, "epoch": 1, "sequence": last_sequence,
                          "items": [{"observation_id": item["observation_id"],
                                     "state": "DIRTY" if item["kind"] == "dirty" else "INDEX_CURRENT"}
                                    for item in request["items"]]}
                with open(args.log, "a", encoding="utf-8") as log:
                    log.write(json.dumps(request, ensure_ascii=False) + "\n")
            output = json.dumps(result).encode()
            response_stamp = str(int(time.time()))
            response_nonce = hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:32]
            response_hash = hashlib.sha256(output).hexdigest()
            signature = hmac.new(secret, f"POST\n{self.path}\n{response_stamp}\n{response_nonce}\n{response_hash}".encode(), hashlib.sha256).hexdigest()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(output)))
            for key, value in {"X-DocMind-Key-Id": "test-key", "X-DocMind-Timestamp": response_stamp,
                               "X-DocMind-Nonce": response_nonce, "X-DocMind-Content-SHA256": response_hash,
                               "X-DocMind-Signature": signature}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(output)

    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
