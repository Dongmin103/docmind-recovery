"""Deployment boundary and corruption checks for DOCMIND packages."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("package_docmind", Path(__file__).resolve().parents[1] / "package_docmind.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


class PackageTests(unittest.TestCase):
    def test_runtime_and_private_boundaries(self):
        config = json.loads((package.ROOT / "deploy/package.json").read_text())
        for name in ("app/api/ragflow_server.py", "app/rag/nlp/docmind_lexical.py", "app/web/package-lock.json", "app/docker/.env.docmind.cpu.example"):
            self.assertTrue(package.selected(name, config), name)
        for name in ("app/sdk/python/ragflow_sdk/__init__.py", "app/example/http/example.sh", "app/helm/Chart.yaml", "app/web/src/pages/docmind/answer-panel.test.tsx", "tools/windows/Test-DocMindSyncQueue.ps1", "app/conf/service_conf.yaml", "app/conf/private.pem", "app/docker/.env", "app/docker/.env.docmind.cpu", "app/runtime/input.docx", "app/web/node_modules/a.js", ".local/key.json", "../secret", "C:/secret", "app/api/../secret", "app/secret\\file"):
            self.assertFalse(package.selected(name, config), name)

    def make_zip(self, path, name="app/api/server.py", content=b"print('DOCMIND')\n", corrupt=False, extra=False, duplicate=False):
        manifest = {"schema": "docmind-package-v1", "source_commit": "a" * 40, "files": [{"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}]}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(name, b"corrupted" if corrupt else content)
            archive.writestr(package.MANIFEST, json.dumps(manifest))
            if extra:
                archive.writestr("unexpected.txt", "unexpected")
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, content)

    def test_valid_package(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.zip"
            self.make_zip(path)
            self.assertTrue(package.verify(path)["verified"])

    def test_reject_corrupt_added_and_duplicate_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.zip"
            for kwargs in ({"corrupt": True}, {"extra": True}, {"duplicate": True}):
                with self.subTest(kwargs=kwargs):
                    self.make_zip(path, **kwargs)
                    with self.assertRaises(ValueError):
                        package.verify(path)

    def test_reject_private_and_traversal_even_with_matching_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package.zip"
            for name in ("../escape.py", "app/conf/private.pem", "app/conf/service_conf.yaml", "app/docker/.env"):
                with self.subTest(name=name):
                    self.make_zip(path, name=name)
                    with self.assertRaises(ValueError):
                        package.verify(path)


if __name__ == "__main__":
    unittest.main()
