"""Load real contract modules in the standalone trial, without app startup.

Only the parser_platform package initializer is bypassed: it eagerly imports the
HTTP bridge and server settings. Schema/identity/adapter source files are unmodified.
This does not test production dispatch or server initialization.
"""

import sys
import types
from pathlib import Path


def load_contracts():
    app = Path(__file__).resolve().parents[2] / "app"
    if str(app) not in sys.path:
        sys.path.insert(0, str(app))
    if "rag.parser_platform" not in sys.modules:
        package = types.ModuleType("rag.parser_platform")
        package.__path__ = [str(app / "rag/parser_platform")]
        sys.modules[package.__name__] = package
