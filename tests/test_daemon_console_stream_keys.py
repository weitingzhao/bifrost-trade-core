"""Strategy Trading Daemon console: merged stream keys for Monitor (dev/prod + legacy)."""

import pytest

from bifrost_core.config.yaml_config import daemon_trading_console_stream_key

pytestmark = pytest.mark.skip(reason="M5: requires bifrost_api.monitor logs router")


def test_daemon_trading_console_stream_key_suffix() -> None:
    assert daemon_trading_console_stream_key(None) == "bifrost:console:dev:daemon_trading"
    assert daemon_trading_console_stream_key("prod") == "bifrost:console:prod:daemon_trading"
