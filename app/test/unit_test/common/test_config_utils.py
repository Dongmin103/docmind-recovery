#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#

from common.config_utils import _redact_config_value


def test_redact_config_value_recursively_masks_secrets():
    value = {
        "config": {
            "password": "database-password",
            "nested": [{"api_key": "api-key"}, {"safe": "visible"}],
        },
        "client_secret": "oauth-secret",
        "max_tokens": 4096,
    }

    redacted = _redact_config_value(value)

    assert redacted["config"]["password"] == "********"
    assert redacted["config"]["nested"][0]["api_key"] == "********"
    assert redacted["config"]["nested"][1]["safe"] == "visible"
    assert redacted["client_secret"] == "********"
    assert redacted["max_tokens"] == 4096
    assert value["config"]["password"] == "database-password"


def test_redact_config_value_masks_top_level_secret():
    assert _redact_config_value("private-material", "private_key") == "********"
