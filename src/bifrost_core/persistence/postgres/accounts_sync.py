"""Normalize and write accounts_snapshot into account and account_positions tables.

Used by PostgreSQLSink (write_snapshot) and by the legacy reader. See docs/DATABASE.md.
"""

import math
from typing import Any, Dict, List, Optional, Tuple

from psycopg2.extras import Json


def _has_meaningful_commission(v: Any, is_numeric: bool = True) -> bool:
    """是否有意义的 commission 相关值（非 None，数值非 0，字符串非空）。"""
    if v is None:
        return False
    if is_numeric and v == 0:
        return False
    if not is_numeric and (not v or not str(v).strip()):
        return False
    return True


def _json_safe(obj: Any) -> Any:
    """Return a JSON-serializable copy (nan/inf -> None) so psycopg2 Json() and jsonb never fail."""
    if obj is None:
        return None
    if isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if math.isfinite(obj):
            return obj
        return None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return str(obj)


def _parse_summary_floats(
    summary: Dict[str, Any],
) -> Tuple[Optional[float], Optional[float], Optional[float], Dict[str, Any]]:
    """Extract net_liquidation, total_cash, buying_power from IB summary; return (nl, tc, bp, summary_extra)."""
    if not summary or not isinstance(summary, dict):
        return None, None, None, {}
    extra = dict(summary)
    nl = tc = bp = None
    for key, val in list(extra.items()):
        if val is None or val == "":
            continue
        try:
            f = float(val) if not isinstance(val, (int, float)) else float(val)
            if not math.isfinite(f):
                continue
            if key == "NetLiquidation":
                nl = f
                del extra[key]
            elif key == "TotalCashValue":
                tc = f
                del extra[key]
            elif key == "BuyingPower":
                bp = f
                del extra[key]
        except (TypeError, ValueError):
            pass
    return nl, tc, bp, extra


def sync_accounts_snapshot_to_tables(
    conn, accounts_list: Optional[List[Dict[str, Any]]]
) -> None:
    """Write normalized accounts_snapshot into account + account_positions.
    account: upsert by account_id. account_positions: upsert by (account_id, symbol, sec_type);
    only delete rows for an account that are no longer in the snapshot (position closed).
    """
    if not accounts_list or not isinstance(accounts_list, list):
        return
    with conn.cursor() as cur:
        for acc in accounts_list:
            if not isinstance(acc, dict):
                continue
            account_id = acc.get("account_id") or acc.get("account")
            if not account_id:
                continue
            account_id = str(account_id).strip()
            summary = acc.get("summary") or {}
            if not isinstance(summary, dict):
                summary = {}
            net_liq, total_cash, buying_power, summary_extra = _parse_summary_floats(
                summary
            )
            summary_extra_json = _json_safe(summary_extra) if summary_extra else None
            # account: upsert by account_id (no delete)
            cur.execute(
                """
                INSERT INTO account (account_id, updated_at, net_liquidation, total_cash, buying_power, summary_extra)
                VALUES (%s, now(), %s, %s, %s, %s)
                ON CONFLICT (account_id) DO UPDATE SET
                    updated_at = now(),
                    net_liquidation = EXCLUDED.net_liquidation,
                    total_cash = EXCLUDED.total_cash,
                    buying_power = EXCLUDED.buying_power,
                    summary_extra = EXCLUDED.summary_extra
                """,
                (
                    account_id,
                    net_liq,
                    total_cash,
                    buying_power,
                    (
                        Json(summary_extra_json)
                        if summary_extra_json is not None
                        else None
                    ),
                ),
            )
            # account_positions: upsert by (account_id, contract_key); contract_key distinguishes OPT by expiry/strike/right
            positions = acc.get("positions") or []
            seen_keys: List[str] = []
            if isinstance(positions, list):
                for p in positions:
                    if not isinstance(p, dict):
                        continue
                    sym = p.get("symbol") or ""
                    sec = p.get("secType") or p.get("sec_type") or ""
                    ex = p.get("exchange") or ""
                    curr = p.get("currency") or ""
                    pos_val = p.get("position")
                    try:
                        pos_f = float(pos_val) if pos_val is not None else None
                    except (TypeError, ValueError):
                        pos_f = None
                    if pos_f is not None and not math.isfinite(pos_f):
                        pos_f = None
                    avg = p.get("avgCost") or p.get("avg_cost")
                    try:
                        avg_f = float(avg) if avg is not None else None
                    except (TypeError, ValueError):
                        avg_f = None
                    if avg_f is not None and not math.isfinite(avg_f):
                        avg_f = None
                    exp = p.get("lastTradeDateOrContractMonth") or p.get("expiry") or ""
                    strike_raw = p.get("strike")
                    try:
                        strike_f = float(strike_raw) if strike_raw is not None else None
                    except (TypeError, ValueError):
                        strike_f = None
                    if strike_f is not None and not math.isfinite(strike_f):
                        strike_f = None
                    rt = p.get("right") or ""
                    if sec == "OPT":
                        contract_key = f"{sym}|{sec}|{exp}|{strike_f}|{rt}"
                    else:
                        contract_key = f"{sym}|{sec}|||"
                    cur.execute(
                        """
                        INSERT INTO account_positions (account_id, symbol, sec_type, exchange, currency, position, avg_cost, expiry, strike, option_right, contract_key, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (account_id, contract_key) DO UPDATE SET
                            exchange = EXCLUDED.exchange,
                            currency = EXCLUDED.currency,
                            position = EXCLUDED.position,
                            avg_cost = EXCLUDED.avg_cost,
                            expiry = EXCLUDED.expiry,
                            strike = EXCLUDED.strike,
                            option_right = EXCLUDED.option_right,
                            updated_at = now()
                        """,
                        (
                            account_id,
                            sym,
                            sec,
                            ex,
                            curr,
                            pos_f,
                            avg_f,
                            exp or None,
                            strike_f,
                            rt or None,
                            contract_key,
                        ),
                    )
                    seen_keys.append(contract_key)
            # Remove positions for this account that are no longer in snapshot (closed)
            if seen_keys:
                cur.execute(
                    """
                    DELETE FROM account_positions
                    WHERE account_id = %s AND (contract_key IS NULL OR contract_key != ALL(%s::text[]))
                    """,
                    (account_id, seen_keys),
                )
            else:
                cur.execute(
                    "DELETE FROM account_positions WHERE account_id = %s", (account_id,)
                )
