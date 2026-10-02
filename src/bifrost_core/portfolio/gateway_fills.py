"""IB Gateway plugin fills -> the row ``write_account_executions_to_db`` writes (core 0.35.0).

``POST /executions/fetch`` (api) asks the Platform IB Gateway plugin for ``fetch_executions``
and passed its rows straight to ``accounts.write_account_executions_to_db``. The two never
agreed on names: the plugin answers ``account`` / ``shares`` / ``ts`` and no ``source``; the
writer reads ``account_id`` / ``quantity`` / ``time`` / ``source``. So every fetched fill
was stored with NULL account, quantity, time and source (six rows on Golden Source, from
2026-08-08; they are left as they are).

The plugin row today (``bifrost_plugin/ib_gateway/ib_ops.py``)::

    exec_id, account, symbol, sec_type, side, shares, price, commission, realized_pnl, ts

It has no option fields (expiry, strike, right, local symbol, conId, multiplier) and its
``symbol`` is the contract's underlying symbol, so an option fill cannot get a
``contract_key``. Such a fill is refused rather than written half-empty: the raw TWS insert
is ``ON CONFLICT (exec_id) DO NOTHING``, so a keyless row would also block the complete row
for the same fill later. The mapping passes the option fields through once the plugin sends
them. A stock fill gets ``SYMBOL|STK|||``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Tuple

from bifrost_core.portfolio.contract_key import stk_key

# The value every TWS fill already on Golden Source carries (raw_broker.executions_raw_tws).
GATEWAY_FILL_SOURCE = "tws_client"

# plugin name -> writer name
_RENAMES = (("account", "account_id"), ("shares", "quantity"), ("ts", "time"))
# copied when present (the writer's own names; the plugin may add them later)
_PASSTHROUGH = (
    "exec_id", "account_id", "time", "symbol", "sec_type", "side", "quantity", "price",
    "commission", "realized_pnl", "currency", "expiry", "strike", "option_right", "exchange",
    "order_id", "cum_qty", "contract_key", "conid", "multiplier",
)
# a row without these is not a fill the book can use
REQUIRED = ("exec_id", "account_id", "side", "quantity")
# and an option fill also needs its contract
REQUIRED_OPTION = ("expiry", "strike", "option_right")
_OPTION_SEC_TYPES = ("OPT", "FOP")


def execution_row_from_gateway_fill(fill: Mapping[str, Any]) -> Dict[str, Any]:
    """One plugin fill as a writer row. Writer names win over plugin names when both are set."""
    row: Dict[str, Any] = {k: fill.get(k) for k in _PASSTHROUGH if fill.get(k) is not None}
    for plugin_name, writer_name in _RENAMES:
        if row.get(writer_name) is None and fill.get(plugin_name) is not None:
            row[writer_name] = fill.get(plugin_name)
    row["source"] = fill.get("source") or GATEWAY_FILL_SOURCE
    sym = str(row.get("symbol") or "").strip()
    sec = str(row.get("sec_type") or "").strip().upper()
    if not row.get("contract_key") and sym and sec == "STK":
        row["contract_key"] = stk_key(sym.upper())
    return row


def _missing(row: Mapping[str, Any]) -> List[str]:
    need = REQUIRED
    if str(row.get("sec_type") or "").strip().upper() in _OPTION_SEC_TYPES:
        need = REQUIRED + REQUIRED_OPTION
    return [k for k in need if row.get(k) is None or str(row.get(k)).strip() == ""]


def execution_rows_from_gateway_fills(
    fills: Iterable[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Map every fill; return ``(rows to write, rows refused)``.

    A refused row lacks one of ``REQUIRED`` (an option fill also ``REQUIRED_OPTION``) after
    mapping; it is returned as
    ``{"exec_id", "missing": [...]}`` so the caller can say so instead of writing a NULL row.
    """
    ok: List[Dict[str, Any]] = []
    refused: List[Dict[str, Any]] = []
    for fill in fills:
        if not isinstance(fill, Mapping):
            refused.append({"exec_id": None, "missing": list(REQUIRED)})
            continue
        row = execution_row_from_gateway_fill(fill)
        missing = _missing(row)
        if missing:
            refused.append({"exec_id": row.get("exec_id"), "missing": missing})
        else:
            ok.append(row)
    return ok, refused
