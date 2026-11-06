"""Bounded CPU embedding process pool for the AVDC capacity trial."""
import argparse
import hmac
import http.client
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def validate_groups(groups, available):
    if not isinstance(groups, list) or not 1 <= len(groups) <= 24:
        raise ValueError("Expected one to twenty-four worker CPU groups")
    seen = set()
    for group in groups:
        if not isinstance(group, list) or len(group) != 4:
            raise ValueError("Each worker requires four logical CPUs")
        if any(type(cpu) is not int or cpu < 0 for cpu in group):
            raise ValueError("Invalid logical CPU")
        if len(set(group)) != 4 or seen.intersection(group):
            raise ValueError("Worker CPU groups must be disjoint")
        if not set(group) <= available:
            raise ValueError("Worker CPU outside container affinity")
        seen.update(group)
    return groups


def healthy(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except OSError:
        return False
    finally:
        connection.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(240)

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            self.reply(404, b'{"error":"Not found"}')
            return
        ready = all(process.poll() is None for process in self.server.workers)
        ready = ready and all(healthy(port) for port in self.server.ports)
        self.reply(200 if ready else 503, json.dumps({
            "status": "ok" if ready else "unavailable",
            "model": "gte-multilingual-base", "dimensions": 768,
            "device": "cpu", "workers": len(self.server.ports),
        }).encode())

    def do_POST(self):
        if self.path != "/v1/embeddings":
            self.reply(404, b'{"error":"Not found"}')
            return
        authorization = self.headers.get("Authorization", "")
        if not hmac.compare_digest(authorization, "Bearer " + self.server.key):
            self.reply(401, b'{"error":"Unauthorized"}')
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.reply(400, b'{"error":"Invalid request size"}')
            return
        if not 0 < size <= 8 * 1024 * 1024:
            self.reply(413, b'{"error":"Invalid request size"}')
            return
        body = self.rfile.read(size)
        if len(body) != size:
            self.reply(400, b'{"error":"Incomplete request"}')
            return
        try:
            port = self.server.available.get(timeout=180)
        except queue.Empty:
            self.reply(503, b'{"error":"Embedding queue busy"}')
            return
        started = time.time()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=240)
        status = 503
        reply = b'{"error":"Embedding worker unavailable"}'
        try:
            connection.request("POST", self.path, body=body, headers={
                "Content-Type": "application/json", "Authorization": authorization,
            })
            response = connection.getresponse()
            status, reply = response.status, response.read()
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
            self.server.available.put(port)
            print(json.dumps({"worker": port, "start": started,
                              "end": time.time(), "status": status}), flush=True)
        try:
            self.reply(status, reply)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    assert config["device"] == "cpu" and config["threads"] == 4
    assert config["cpu_short_batch"] == 16
    key = Path(config["api_key_file"]).read_text().strip()
    assert key
    groups = validate_groups(config["cpu_groups"], os.sched_getaffinity(0))
    ports = tuple(range(8081, 8081 + len(groups)))
    workers = []
    server = None
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    try:
        for index, (cores, port) in enumerate(zip(groups, ports)):
            assert set(cores) <= os.sched_getaffinity(0)
            child_config = dict(config, bind="127.0.0.1", port=port)
            path = Path("/tmp") / ("embedding-worker-" + str(index) + ".json")
            path.write_text(json.dumps(child_config))
            path.chmod(0o600)
            program = (
                "import os,runpy,sys;"
                "os.sched_setaffinity(0," + repr(set(cores)) + ");"
                "sys.argv=['service.py','--config'," + repr(str(path)) + "];"
                "runpy.run_path('/opt/docmind/service.py',run_name='__main__')"
            )
            workers.append(subprocess.Popen([sys.executable, "-c", program]))
        deadline = time.monotonic() + 240
        while not all(healthy(port) for port in ports):
            if any(process.poll() is not None for process in workers):
                raise RuntimeError("Embedding worker exited during startup")
            if time.monotonic() > deadline:
                raise TimeoutError("Embedding worker startup timed out")
            time.sleep(1)
        server = ThreadingHTTPServer((config.get("bind", "0.0.0.0"),
                                     config.get("port", 8080)), Handler)
        server.ports = ports
        server.key = key
        server.workers = workers
        server.available = queue.Queue()
        for port in ports:
            server.available.put(port)
        print(str(len(groups)) + " embedding workers ready", flush=True)
        server.serve_forever()
    finally:
        if server is not None:
            server.server_close()
        for process in workers:
            if process.poll() is None:
                process.terminate()
        for process in workers:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
