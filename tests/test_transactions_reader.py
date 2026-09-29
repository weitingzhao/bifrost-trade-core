"""get_transactions returns the security a cash row is about (0.25.3)."""

from bifrost_core.portfolio.reader.executions import get_transactions


class _Cursor:
    def __init__(self, sink, rows):
        self.sink = sink
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, q, args):
        self.sink.append((q, list(args)))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.sql = []
        self.rows = rows

    def cursor(self, cursor_factory=None):
        return _Cursor(self.sql, self.rows)


def test_selects_symbol_and_conid():
    row = {"account_transactions_id": 1, "account_id": "U1", "ts": 1.0, "amount": -3.5, "type": "other",
           "currency": "USD", "description": "withholding", "created_at": None, "symbol": "ABC", "conid": 42}
    conn = _Conn([row])
    out = get_transactions(conn, account_id="U1", limit=5)
    q, args = conn.sql[0]
    select = q.split("FROM")[0]
    assert "symbol" in select and "conid" in select
    assert args == ["U1", 5]
    assert out == [row]


def test_no_connection_is_empty():
    assert get_transactions(None) == []
