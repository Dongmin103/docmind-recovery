"""Keep service unit tests independent of optional storage/search SDKs."""

import sys
import types

import common

fake_settings = types.ModuleType("common.settings")
fake_settings.DATABASE_TYPE = "MYSQL"
fake_settings.DATABASE = {
    "name": "docmind-unit",
    "host": "127.0.0.1",
    "port": 3306,
    "user": "unit",
    "password": "unit",
}
fake_settings.get_secret_key = lambda: "unit-test-secret"
fake_settings.init_settings = lambda: None
fake_settings.decrypt_database_config = lambda **_kwargs: {}
common.settings = fake_settings
sys.modules["common.settings"] = fake_settings
