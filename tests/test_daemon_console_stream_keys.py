"""Strategy Trading Daemon console: merged stream keys for Monitor (dev/prod + legacy)."""

import pytest

pytestmark = pytest.mark.skip(reason="M5: requires bifrost_api.monitor logs router")
from bifrost_core.config.yaml_config import daemon_trading_console_stream_key


def test_daemon_trading_console_stream_key_suffix() -> None:
    assert daemon_trading_console_stream_key(None) == "bifrost:console:dev:daemon_trading"
    assert daemon_trading_console_stream_key("prod") == "bifrost:console:prod:daemon_trading"


def test_daemon_console_stream_keys_for_read_fixed_list() -> None:
    keys = _daemon_console_stream_keys_for_read()
    assert keys == [
        "bifrost:console:daemon_trading",
        "bifrost:console:dev:daemon_trading",
        "bifrost:console:prod:daemon_trading",
    ]
