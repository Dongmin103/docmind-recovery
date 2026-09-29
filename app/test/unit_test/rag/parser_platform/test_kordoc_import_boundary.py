"""The active parser package must not load retired search engines on import."""

import subprocess
import sys
import unittest


class KordocImportBoundaryTest(unittest.TestCase):
    def test_parser_platform_package_does_not_load_retired_engines(self):
        probe = """
import sys
import rag.parser_platform
retired = (
    'rag.parser_platform.surya_',
    'rag.parser_platform.docling_',
    'rag.parser_platform.rhwp_',
    'rag.parser_platform.office_media',
)
loaded = [name for name in sys.modules if name.startswith(retired)]
assert not loaded, loaded
"""
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
