"""Golden test for the contract_key builders (TD-25): no persisted key format may change.

Before TD-25 core built option keys inline in five places (positions sync, the daemon's
execution sink, the api's execution write, the read-time fallback for rows without a key,
and the positions -> executions join variants). Verbatim copies of each are under
``golden_oracles/contract_key_legacy.py``; ``fixtures/golden_contract_key.json`` holds their
output over the matrix below.

Checks:

1. the oracles still produce the fixture (frozen copies, so this guards the copy);
2. core's functions, called directly, return the same strings as the oracles -- except the
   read-time fallback for a fractional strike, which TD-25 fixes on purpose (82.5 was "82");
3. the three write paths, driven through a fake connection, write the same contract_key
   into the INSERT as the oracle computes -- byte for byte, per source.

Regenerate the fixture only on purpose:  PYTHONPATH=src python tests/test_golden_contract_key.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from golden_oracles import contract_key_legacy as legacy
from write_fakes import FakeConn

from bifrost_core.persistence.postgres import accounts_sync
from bifrost_core.persistence.postgres.postgres_sink import TradingDaemonSink
from bifrost_core.portfolio import contract_key
from bifrost_core.portfolio.reader import accounts, accounts_helpers, executions

FIXTURE = Path(__file__).parent / "fixtures" / "golden_contract_key.json"

STRIKES: tuple = (80, 80.0, 82.5, 0.5, 1234.125, 7.75, None, "82.5")
EXPIRIES: tuple = ("20260320", "2026-03-20", 20260320)
RIGHTS = ("C", "P", "CALL", "PUT", "")
SYMBOLS = ("RKLB", "SPY", "F", "GOOGL", "BRK B", "BF.B")
SOURCES = ("tws_event", "tws_client", "flex_trades", "manual")
# The builders only ask "is it TWS?", so the fixture keeps one source of each kind; the
# write-path tests below still run all four against the oracle.
FIXTURE_SOURCES = ("tws_event", "manual")

GIVEN_KEY = "GIVEN|OPT|20260320|80.0|C"


def matrix() -> List[tuple]:
    return [(sym, exp, k, rt) for sym in SYMBOLS for exp in EXPIRIES for k in STRIKES for rt in RIGHTS]


def _label(v: Any) -> str:
    return f"{type(v).__name__}:{v}"


def _safe(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - the class name is the recorded behaviour
        return {"error": type(exc).__name__}


def _position(sym: str, exp: Any, k: Any, rt: str, sec: str = "OPT") -> Dict[str, Any]:
    """An IB positions snapshot row as accounts_sync reads it."""
    return {"symbol": sym, "secType": sec, "lastTradeDateOrContractMonth": exp, "strike": k,
            "right": rt, "position": 1, "avgCost": 1.0}


def _fill(fn: Any, row: Dict[str, Any]) -> Optional[str]:
    d = dict(row)
    fn(d)
    return d.get("contract_key")


def _fill_row(sym: str, exp: Any, k: Any, rt: str) -> Dict[str, Any]:
    return {"sec_type": "OPT", "symbol": sym, "expiry": exp, "strike": k, "option_right": rt}


# Rows outside the matrix that reach the other branches of each builder.
FILL_EXTRA: List[Dict[str, Any]] = [
    {"sec_type": "OPT", "symbol": "RKLB", "expiry": "20260320", "strike": 82.5, "option_right": "C",
     "contract_key": "KEEP|OPT|20260320|82.5|C"},
    {"sec_type": "STK", "symbol": "RKLB"},
    {"sec_type": "opt", "symbol": " RKLB ", "expiry": 20260320.0, "strike": float("nan"), "option_right": "c"},
    {"sec_type": "OPT", "symbol": "RKLB", "expiry": None, "strike": float("inf"), "option_right": None, "right": "put"},
    {"sec_type": "OPT", "symbol": "RKLB", "expiry": "2026-03-20", "strike": " 82.5 ", "option_right": "X"},
    {"sec_type": "OPT", "symbol": "RKLB", "expiry": "20260320", "strike": -2.5, "option_right": "CALLS"},
    {"sec_type": "OPT", "symbol": "RKLB", "expiry": "20260320", "strike": 80.25, "option_right": "", "right": ""},
    {"sec_type": "OPT", "symbol": "RKLB", "expiry": "20260320", "strike": 1e-9, "option_right": "P"},
    {"sec_type": "OPT", "symbol": "RKLB", "expiry": "20260320", "strike": 0, "option_right": "P"},
]

VARIANT_EXTRA = [
    "",
    "RKLB|STK|||",
    "RKLB  260320C00080000|OPT|20260320|80.0|C",
    "RKLB|OPT|20260320|abc|C",
    "RKLB|OPT|2026032|80|X",
    " rklb |OPT|260320|80.50|p ",
    "BRK B|OPT|20260320|410.0|P",
    "RKLB|OPT|20260320|80.0",
    "RKLB|OPT|20260320|1e2|C",
]

# (symbol, sec_type, source, expiry, strike, option_right, contract_key) outside the matrix.
EXEC_EXTRA: List[tuple] = [
    ("RKLB", "STK", "tws_event", None, None, None, "RKLB|STK|||"),
    ("RKLB", "opt", "tws_event", "20260320", 80, "c", None),
    ("RKLB", "OPT", " tws_client ", "20260320", 80, "C", None),
    ("RKLB", "OPT", "tws_event", "20260320", 80, "C", GIVEN_KEY),
    ("RKLB", "OPT", "flex_trades", "20260320", 80, "C", GIVEN_KEY),
    ("RKLB", "OPT", "tws_event", "", 80, "C", None),
    ("RKLB", "OPT", "tws_event", "260320", 80, "C", None),
    ("RKLB", "OPT", "tws_event", "20260320", "", "C", None),
    ("RKLB", "OPT", "tws_event", "20260320", "abc", "C", None),
    ("RKLB", "OPT", "tws_event", "20260320", float("inf"), "C", None),
    ("RKLB", "OPT", "tws_event", "20260320", float("nan"), "C", None),
    ("RKLB", "OPT", "tws_event", "20260320", -1.5, "P", None),
    ("RKLB", "OPT", "tws_event", "20260320", 80, "X", None),
    ("", "OPT", "manual", "20260320", 80, "C", None),
    (None, "OPT", "tws_event", None, None, None, None),
    (" RKLB ", "OPT", "manual", 20260320.0, 82.5, "PUT", None),
]

POSITION_EXTRA: List[Dict[str, Any]] = [
    {"symbol": "RKLB", "secType": "STK", "position": 10, "avgCost": 1.0},
    {"symbol": "ES", "sec_type": "FUT", "expiry": "20261218", "position": 1, "avgCost": 1.0},
    {"symbol": "RKLB", "secType": "OPT", "expiry": "20260320", "strike": float("nan"), "right": "C",
     "position": 1, "avgCost": 1.0},
    {"symbol": "RKLB", "secType": "OPT", "expiry": "20260320", "strike": "abc", "right": "C",
     "position": 1, "avgCost": 1.0},
    {"symbol": "RKLB", "secType": "opt", "expiry": "20260320", "strike": 80, "right": "C",
     "position": 1, "avgCost": 1.0},
    {"symbol": None, "secType": "OPT", "position": 1, "avgCost": 1.0},
]


def build() -> Dict[str, Any]:
    cases = []
    for sym, exp, k, rt in matrix():
        pos = legacy.positions_key(_position(sym, exp, k, rt))
        cases.append(
            {
                "in": [sym, _label(exp), _label(k), rt],
                "positions": pos,
                "fill": _fill(legacy.fill_contract_key_for_opt, _fill_row(sym, exp, k, rt)),
                "sink": [legacy.sink_execution_key(sym, "OPT", s, exp, k, rt, None) for s in FIXTURE_SOURCES],
                "accounts": [
                    legacy.accounts_execution_key(sym, "OPT", s, exp, k, rt, None) for s in FIXTURE_SOURCES
                ],
                "osi": _safe(legacy.occ_local_symbol, sym, str(exp), k, rt),
                "variants": legacy.contract_key_variants(pos),
            }
        )
    return {
        "about": "TD-25 golden keys; see tests/test_golden_contract_key.py",
        "sources": list(FIXTURE_SOURCES),
        "matrix": {k: [c[k] for c in cases] for k in cases[0]},  # one list per output
        "fill_extra": [_fill(legacy.fill_contract_key_for_opt, row) for row in FILL_EXTRA],
        "variant_extra": [legacy.contract_key_variants(ck) for ck in VARIANT_EXTRA],
        "sink_extra": [legacy.sink_execution_key(*e) for e in EXEC_EXTRA],
        "accounts_extra": [legacy.accounts_execution_key(*e) for e in EXEC_EXTRA],
        "positions_extra": [legacy.positions_key(p) for p in POSITION_EXTRA],
    }


@pytest.fixture(scope="module")
def golden() -> Dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def test_fixture_covers_the_matrix(golden: Dict[str, Any]) -> None:
    assert len(golden["matrix"]["positions"]) == len(matrix()) == 720
    keys = set(golden["matrix"]["positions"])
    # The documented persisted formats, spelled out once.
    assert "RKLB|OPT|20260320|80.0|C" in keys
    assert "RKLB|OPT|20260320|82.5|P" in keys
    assert "RKLB|OPT|20260320|None|C" in keys  # positions write the text None for no strike
    sink = {k for pair in golden["matrix"]["sink"] for k in pair}
    assert "RKLB  260320C00080000|OPT|20260320|80.0|C" in sink  # TWS: 2-space legacy root
    assert "RKLB|OPT|20260320||C" in sink  # non-TWS fallback: empty text for no strike


def test_oracles_reproduce_the_fixture(golden: Dict[str, Any]) -> None:
    assert build() == golden


# --- core, called directly --------------------------------------------------------------


def test_occ_local_symbol_matches_the_oracle() -> None:
    # core 0.46.0 (TD-80) deleted executions' position-vs-execution key variants with their only
    # reader (get_executions_by_contract_keys); the oracle keeps them so the fixture is unchanged.
    for sym, exp, k, rt in matrix():
        assert _safe(executions._occ_local_symbol, sym, str(exp), k, rt) == _safe(
            legacy.occ_local_symbol, sym, str(exp), k, rt
        )


def _fallback_expected(row: Dict[str, Any]) -> Optional[str]:
    """The legacy read-time key, with the one intended change (TD-25 step C): a fractional
    numeric strike prints in full instead of being cut by int(). Integral strikes keep the
    legacy "80" (not "80.0"), so no key that could already have been seen changes."""
    key = _fill(legacy.fill_contract_key_for_opt, row)
    k = row.get("strike")
    if (
        key is not None
        and not row.get("contract_key")
        and isinstance(k, (int, float))
        and math.isfinite(k)
        and k != int(k)
    ):
        parts = key.split("|")
        parts[3] = str(float(k))
        key = "|".join(parts)
    return key


def test_read_fallback_matches_the_oracle_except_the_truncation() -> None:
    changed = 0
    for sym, exp, k, rt in matrix():
        row = _fill_row(sym, exp, k, rt)
        got = _fill(accounts_helpers._fill_contract_key_for_opt, row)
        assert got == _fallback_expected(row), row
        changed += got != _fill(legacy.fill_contract_key_for_opt, row)
    for row in FILL_EXTRA:
        assert _fill(accounts_helpers._fill_contract_key_for_opt, row) == _fallback_expected(row), row
    # 82.5, 0.5, 1234.125 and 7.75: 4 of 8 strikes x 3 expiries x 5 rights x 6 symbols.
    assert changed == 4 * 3 * 5 * 6


def test_read_fallback_spells_fractional_strikes_in_full() -> None:
    def key(strike: Any) -> Optional[str]:
        return contract_key.read_fallback_opt_key(_fill_row("RKLB", "2026-03-20", strike, "CALL"))

    assert key(82.5) == "RKLB|OPT|20260320|82.5|C"  # was RKLB|OPT|20260320|82|C
    assert key(82) == "RKLB|OPT|20260320|82|C"  # ... which is the 82 strike
    assert key(80) == key(80.0) == "RKLB|OPT|20260320|80|C"  # unchanged
    assert key(None) == "RKLB|OPT|20260320||C"  # unchanged (the 2 rows that use it today)
    assert key("82.5") == "RKLB|OPT|20260320|82.5|C"  # unchanged
    assert contract_key.read_fallback_opt_key({"sec_type": "OPT", "contract_key": "X"}) is None
    assert contract_key.read_fallback_opt_key({"sec_type": "STK", "symbol": "RKLB"}) is None


def test_builders_match_the_oracle() -> None:
    for sym, exp, k, rt in matrix():
        assert _safe(contract_key.osi_local_symbol, sym, str(exp), k, rt) == _safe(
            legacy.occ_local_symbol, sym, str(exp), k, rt
        )
        for s in SOURCES:
            want = legacy.accounts_execution_key(sym, "OPT", s, exp, k, rt, None)
            if s in contract_key.TWS_SOURCES:
                assert contract_key.tws_execution_opt_key(sym, exp, k, rt) == want
            else:
                assert want is None
        p = _position(sym, exp, k, rt)
        sf = None if k is None else float(k)
        assert contract_key.opt_key(sym, exp, sf, rt, none_text="None") == legacy.positions_key(p)
    assert contract_key.stk_key("RKLB") == legacy.positions_key(POSITION_EXTRA[0]) == "RKLB|STK|||"
    assert contract_key.stk_key("ES", "FUT") == legacy.positions_key(POSITION_EXTRA[1])
    assert contract_key.legacy_tws_local_symbol("F", "20260320", 7.75, "P") == "F  260320P00007750"
    assert contract_key.osi_local_symbol("F", "20260320", 7.75, "P") == "F     260320P00007750"


# --- the write paths, through a fake connection -----------------------------------------


def _exec_rows() -> List[Dict[str, Any]]:
    rows = []
    for i, (sym, exp, k, rt) in enumerate(matrix()):
        for s in SOURCES:
            rows.append({"exec_id": f"m{i}-{s}", "account_id": "U0000001", "symbol": sym, "sec_type": "OPT",
                         "source": s, "expiry": exp, "strike": k, "option_right": rt, "contract_key": None})
    for i, (sym, sec, s, exp, k, rt, ck) in enumerate(EXEC_EXTRA):
        rows.append({"exec_id": f"x{i}", "account_id": "U0000001", "symbol": sym, "sec_type": sec,
                     "source": s, "expiry": exp, "strike": k, "option_right": rt, "contract_key": ck})
    return rows


def _expected(rows: List[Dict[str, Any]], oracle: Any) -> Dict[str, Any]:
    return {
        r["exec_id"]: oracle(r["symbol"], r["sec_type"], r["source"], r["expiry"], r["strike"],
                             r["option_right"], r["contract_key"])
        for r in rows
    }


def _written_keys(conn: FakeConn) -> Dict[str, Any]:
    """exec_id -> contract_key of every executions INSERT (column 2 and 16 of the 54)."""
    out: Dict[str, Any] = {}
    for sql, params in conn.executed:
        if sql.startswith("INSERT INTO") and "executions_raw" in sql and params and len(params) == 54:
            out.setdefault(params[1], params[15])
    return out


def test_daemon_sink_writes_the_legacy_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _exec_rows()
    sink = TradingDaemonSink.__new__(TradingDaemonSink)
    sink._golden_conn = FakeConn()
    monkeypatch.setattr(TradingDaemonSink, "_ensure_golden_conn", lambda self: True)
    sink.write_account_executions(rows)
    written = _written_keys(sink._golden_conn)
    assert len(written) == len(rows)
    assert written == _expected(rows, legacy.sink_execution_key)


def test_api_execution_write_writes_the_legacy_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = _exec_rows()
    conn = FakeConn()
    monkeypatch.setattr(accounts.ws, "open_conn", lambda *a, **k: conn)
    assert accounts.write_account_executions_to_db({"sink": "postgres"}, rows)
    written = _written_keys(conn)
    assert len(written) == len(rows)
    assert written == _expected(rows, legacy.accounts_execution_key)


def test_positions_sync_writes_the_legacy_keys() -> None:
    positions = [_position(sym, exp, k, rt) for sym, exp, k, rt in matrix()] + POSITION_EXTRA
    conn = FakeConn()
    accounts_sync.sync_accounts_snapshot_to_tables(
        conn, [{"account_id": "U0000001", "summary": {}, "positions": positions}]
    )
    written = [
        params[10] for sql, params in conn.executed
        if sql.startswith("INSERT INTO") and "positions" in sql.split("(")[0]
    ]
    assert written == [legacy.positions_key(p) for p in positions]


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(build(), sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
