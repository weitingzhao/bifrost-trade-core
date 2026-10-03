"""get_ib_config no longer reads the Flex range days (TD-74, core 0.39.0).

Nothing used them (``ib_client_for_api`` never output them); the Flex Query plugin keeps
them in Golden Source ``ops_jobs.flex_settings`` from 0.7.0.
"""

from __future__ import annotations

from typing import Any

from bifrost_core.monitor.reader import settings as settings_module


class _Cursor:
    def __init__(self, parent: "_Conn") -> None:
        self.parent = parent

    def execute(self, sql: str, params: Any = None) -> None:
        self.parent.sql.append(sql)

    def fetchone(self) -> Any:
        return {
            "ib_host_account_id": "U0000001",
            "stream_host_account_id": " U0000002 ",
            "stream_secondary_account_id": None,
        }

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class _Conn:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def cursor(self, cursor_factory: Any = None) -> _Cursor:
        return _Cursor(self)


def test_get_ib_config_reads_accounts_only() -> None:
    conn = _Conn()
    out = settings_module.get_ib_config(conn)
    assert out == {
        "ib_host_account_id": "U0000001",
        "stream_host_account_id": "U0000002",
        "stream_secondary_account_id": None,
    }
    assert "flex_" not in conn.sql[0]
