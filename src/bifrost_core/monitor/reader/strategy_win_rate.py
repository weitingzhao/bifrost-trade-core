"""Aggregate strategy instance results by structure for Win Rate page.

P&L rules are **identical for every** ``strategy_structure_name`` (Iron Condor, Bull Put Spread,
Cash Secured Put, etc.): each instance gets one execution-book ``net_pnl``; cards only **group**
rows by name. There is **no** branch on structure type for ``total_profit`` / ``total_loss``.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, TypedDict

from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader.strategy_instance import list_instances
from bifrost_core.portfolio.model.payoff import RiskPosition, compute_risk_profile
from bifrost_core.portfolio.reader.executions import get_executions
from bifrost_core.portfolio.reader.instance_exec_net_pnl import (
    compute_instance_exec_derived_net_pnl,
    slice_execution_for_instance_opt_view,
)

logger = logging.getLogger(__name__)

_EXEC_FINAL = "account_executions_final"
_ALLOC_TABLE = "account_execution_instance_allocation"


class WinRatePayload(TypedDict, total=False):
    structures: List[Dict[str, Any]]
    totals_all: Optional[Dict[str, Any]]


def _batch_underlying_cost(conn: Any, instance_ids: List[int]) -> Dict[int, float]:
    """Return per-instance SELL OPT underlying cost (same definition as Instance Detail UI).

    Uses **strike × |quantity| × 100** per row (``underlyingCostSellOptUsd`` /
    ``instanceDetailPnlMetrics.ts``), not ``net_cash`` / premium.

    - If ``account_execution_instance_allocation`` exists for the fill: for each instance
      row, ``strike × abs(allocated_quantity) × 100`` (matches sliced execution ``quantity``).
    - Otherwise: ``strike × abs(execution.quantity) × 100`` for ``strategy_instance_id``.

    ``strike`` prefers ``executions.strike``; if null/zero, parses segment 4 of
    ``contract_key`` (``symbol|sec|expiry|strike|right``), same as the frontend fallback.
    """
    if not instance_ids or conn is None:
        return {}
    placeholders = ", ".join(["%s"] * len(instance_ids))
    params = tuple(instance_ids) * 4
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                WITH opt_sell AS (
                    SELECT e.account_executions_id,
                           e.strategy_instance_id AS direct_sid,
                           CASE
                             WHEN e.strike IS NOT NULL AND e.strike > 0 THEN e.strike::double precision
                             WHEN NULLIF(trim(split_part(COALESCE(e.contract_key, ''), '|', 4)), '') IS NOT NULL
                               THEN NULLIF(trim(split_part(COALESCE(e.contract_key, ''), '|', 4)), '')::double precision
                             ELSE NULL::double precision
                           END AS eff_strike,
                           ABS(COALESCE(e.quantity, 0)) AS abs_parent_qty
                    FROM {_EXEC_FINAL} e
                    WHERE upper(trim(COALESCE(e.side, ''))) IN ('SELL', 'SLD', 'S')
                      AND upper(trim(COALESCE(e.sec_type, ''))) = 'OPT'
                      AND (
                          e.strategy_instance_id IN ({placeholders})
                          OR EXISTS (
                              SELECT 1 FROM {_ALLOC_TABLE} a0
                              WHERE a0.account_executions_id = e.account_executions_id
                                AND a0.strategy_instance_id IN ({placeholders})
                          )
                      )
                ),
                opt_sell_ok AS (
                    SELECT * FROM opt_sell
                    WHERE eff_strike IS NOT NULL AND eff_strike > 0
                ),
                weighted_from_alloc AS (
                    SELECT a.strategy_instance_id AS sid,
                           SUM(
                               ABS(COALESCE(a.allocated_quantity, 0))
                               * 100.0
                               * o.eff_strike
                           ) AS cost
                    FROM opt_sell_ok o
                    INNER JOIN {_ALLOC_TABLE} a ON a.account_executions_id = o.account_executions_id
                    WHERE a.strategy_instance_id IN ({placeholders})
                    GROUP BY a.strategy_instance_id
                ),
                weighted_direct AS (
                    SELECT o.direct_sid AS sid,
                           SUM(o.abs_parent_qty * 100.0 * o.eff_strike) AS cost
                    FROM opt_sell_ok o
                    WHERE o.direct_sid IS NOT NULL
                      AND o.direct_sid IN ({placeholders})
                      AND NOT EXISTS (
                          SELECT 1 FROM {_ALLOC_TABLE} y
                          WHERE y.account_executions_id = o.account_executions_id
                      )
                    GROUP BY o.direct_sid
                ),
                combined AS (
                    SELECT sid, cost FROM weighted_from_alloc
                    UNION ALL
                    SELECT sid, cost FROM weighted_direct
                )
                SELECT sid, SUM(cost) AS underlying_cost
                FROM combined
                GROUP BY sid
                """,
                params,
            )
            rows = cur.fetchall()
        return {int(r["sid"]): float(r["underlying_cost"] or 0) for r in rows}
    except Exception as exc:
        logger.debug("_batch_underlying_cost failed: %s", exc)
        return {}


def _safe_avg(values: List[float]) -> Optional[float]:
    finite = [v for v in values if v is not None and math.isfinite(v)]
    return sum(finite) / len(finite) if finite else None


def _parse_opt_right(ex: Dict[str, Any]) -> Optional[str]:
    right = str(ex.get("option_right") or "").strip().upper()
    if right in ("C", "P"):
        return right
    ck = str(ex.get("contract_key") or "").strip()
    if ck:
        parts = ck.split("|")
        if len(parts) >= 5:
            r = parts[4].strip().upper()[:1]
            if r in ("C", "P"):
                return r
    return None


def _parse_opt_strike(ex: Dict[str, Any]) -> Optional[float]:
    try:
        strike = float(ex.get("strike"))
        if math.isfinite(strike) and strike > 0:
            return strike
    except (TypeError, ValueError):
        pass
    ck = str(ex.get("contract_key") or "").strip()
    if ck:
        parts = ck.split("|")
        if len(parts) >= 4:
            try:
                strike = float(parts[3].strip())
                if math.isfinite(strike) and strike > 0:
                    return strike
            except (TypeError, ValueError):
                pass
    return None


def _instance_max_risk_from_executions(sliced: List[Dict[str, Any]], underlying_fallback: float) -> float:
    net_by_key: Dict[str, Dict[str, float]] = {}
    for ex in sliced:
        if str(ex.get("sec_type") or "").upper() != "OPT":
            continue
        right = _parse_opt_right(ex)
        strike = _parse_opt_strike(ex)
        if right is None or strike is None:
            continue
        side = str(ex.get("side") or "").upper()
        qty = abs(float(ex.get("quantity") or 0.0))
        if qty <= 0:
            continue
        price = float(ex.get("price") or 0.0)
        signed_qty = qty if side in ("BUY", "BOT", "B") else -qty
        key = f"{strike}|{right}"
        prev = net_by_key.get(key) or {"strike": strike, "right": right, "qty": 0.0, "total_cost": 0.0}
        prev["qty"] += signed_qty
        prev["total_cost"] += price * qty * (1.0 if signed_qty > 0 else -1.0)
        net_by_key[key] = prev

    positions: List[RiskPosition] = []
    for v in net_by_key.values():
        qty = int(round(v["qty"]))
        if qty == 0:
            continue
        avg_cost = abs(v["total_cost"] / v["qty"]) if abs(v["qty"]) > 1e-9 else 0.0
        positions.append(RiskPosition(strike=float(v["strike"]), right=str(v["right"]), qty=qty, avg_cost=float(avg_cost)))

    if not positions:
        return max(0.0, float(underlying_fallback or 0.0))

    rp = compute_risk_profile(positions, covered_shares=0, underlying_avg_cost=None)
    if rp.max_loss is not None and math.isfinite(rp.max_loss) and rp.max_loss < 0:
        return abs(float(rp.max_loss))
    return max(0.0, float(underlying_fallback or 0.0))


def _batch_max_risk(
    conn: Any,
    instance_ids: List[int],
    fallback_underlying: Dict[int, float],
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
) -> Dict[int, float]:
    if conn is None or not instance_ids:
        return {}
    out: Dict[int, float] = {}
    for sid in instance_ids:
        try:
            raw = get_executions(
                conn,
                since_ts=since_ts,
                until_ts=until_ts,
                account_id=None,
                limit=50000,
                strategy_opportunity_id=None,
                strategy_instance_id=int(sid),
                source_scope="performance_book",
            )
            sliced = [sl for ex in raw if (sl := slice_execution_for_instance_opt_view(ex, int(sid))) is not None]
            out[int(sid)] = round(_instance_max_risk_from_executions(sliced, fallback_underlying.get(int(sid), 0.0)), 2)
        except Exception as exc:
            logger.debug("_batch_max_risk failed for sid=%s: %s", sid, exc)
            out[int(sid)] = round(max(0.0, float(fallback_underlying.get(int(sid), 0.0))), 2)
    return out


def _aggregate_win_rate_metrics(structure_name: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One structure row or the global 'All structures' row from per-instance rows.

    ``structure_name`` is only stored on the returned dict for display; **all** structures use this
    same function with the same rules.

    ``total_profit`` / ``total_loss`` are **sums of instance net PnL** in the win vs loss PnL bands
    (``net_pnl`` > 0 and ``net_pnl`` < 0), not gross max-loss metrics.
    """
    profit = [r for r in rows if r["net_pnl"] > 0]
    loss = [r for r in rows if r["net_pnl"] <= 0]

    profit_investment_raw = sum(r["underlying_cost"] for r in profit)
    loss_investment_raw = sum(r["underlying_cost"] for r in loss)
    profit_investment = round(profit_investment_raw, 2)
    loss_investment = round(loss_investment_raw, 2)
    total_investment = round(profit_investment + loss_investment, 2)
    total_max_risk = round(sum(max(0.0, float(r.get("max_risk") or 0.0)) for r in rows), 2)

    def _pct(r: Dict[str, Any]) -> Optional[float]:
        uc = r["underlying_cost"]
        return r["net_pnl"] / uc * 100 if uc > 0 else None

    profit_pcts = [p for r in profit if (p := _pct(r)) is not None]
    loss_pcts = [p for r in loss if (p := _pct(r)) is not None]
    all_pcts = [p for r in rows if (p := _pct(r)) is not None]

    # P&L band totals: same rule for every structure — sum instance execution nets in each band only.
    profit_nets = [r["net_pnl"] for r in profit]
    loss_nets = [r["net_pnl"] for r in loss]
    neg_nets = [x for x in loss_nets if x < 0]
    total_profit = round(sum(profit_nets), 2)
    total_loss = round(sum(neg_nets), 2) if neg_nets else None
    total_net = round(sum(r["net_pnl"] for r in rows), 2)
    structure_return_pct = round((total_net / total_max_risk) * 100, 2) if total_max_risk > 0 else None

    return {
        "structure_name": structure_name,
        "total_instances": len(rows),
        "profit_trades": len(profit),
        "loss_trades": len(loss),
        "total_profit": total_profit,
        "total_loss": total_loss,
        "profit_investment": profit_investment,
        "loss_investment": loss_investment,
        "total_investment": total_investment,
        "total_max_risk": total_max_risk,
        "structure_return_pct": structure_return_pct,
        "profit_avg_pct": round(_safe_avg(profit_pcts), 2) if _safe_avg(profit_pcts) is not None else None,
        "loss_avg_pct": round(_safe_avg(loss_pcts), 2) if _safe_avg(loss_pcts) is not None else None,
        "single_max_loss_pct": round(min(all_pcts), 2) if all_pcts else None,
        "profit_avg_usd": round(_safe_avg([r["net_pnl"] for r in profit]), 2) if profit else None,
        "loss_avg_usd": round(_safe_avg([r["net_pnl"] for r in loss]), 2) if loss else None,
    }


def compute_win_rate_by_structure(
    conn: Any,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
) -> WinRatePayload:
    """Return per-structure rows plus ``totals_all`` (same metrics over every instance).

    **Grouping only:** ``strategy_structure_name`` decides which card a row appears on; the math
    in ``_aggregate_win_rate_metrics`` does not inspect structure name or id.

    Per-instance ``net_pnl`` is **execution-book Net PnL** (OPT premium ± commission groups, non-OPT
    ``realized_pnl``, prorated option–stock slippage) — same as Instance Detail, not broker summary FIFO.
    For every structure card, ``total_profit`` / ``total_loss`` are the sums of those nets for
    instances with net > 0 vs net < 0 respectively.

    Keys:
      structures: list of per-structure dicts (see ``_aggregate_win_rate_metrics``).
      totals_all: aggregate across all instances (for All structures UI), or None if no data.
    """
    empty: WinRatePayload = {"structures": [], "totals_all": None}
    if conn is None:
        return empty

    instances = list_instances(conn)
    active = [i for i in instances if (i.get("executions_count") or 0) > 0]
    if not active:
        return empty

    ids = [i["strategy_instance_id"] for i in active]
    cost_by_id = _batch_underlying_cost(conn, ids)
    max_risk_by_id = _batch_max_risk(conn, ids, cost_by_id, since_ts=since_ts, until_ts=until_ts)

    structure_rows: Dict[str, List[Dict[str, Any]]] = {}
    all_flat: List[Dict[str, Any]] = []

    for inst in active:
        sid = inst["strategy_instance_id"]
        structure_name = (inst.get("strategy_structure_name") or "Unknown").strip() or "Unknown"

        net_pnl = compute_instance_exec_derived_net_pnl(conn, sid, since_ts=since_ts, until_ts=until_ts)

        underlying_cost = cost_by_id.get(sid, 0.0)
        row = {
            "net_pnl": net_pnl,
            "underlying_cost": underlying_cost,
            "max_risk": max_risk_by_id.get(sid, underlying_cost),
        }
        structure_rows.setdefault(structure_name, []).append(row)
        all_flat.append(row)

    results: List[Dict[str, Any]] = []
    for structure_name, rows in sorted(structure_rows.items()):
        results.append(_aggregate_win_rate_metrics(structure_name, rows))

    totals_all = _aggregate_win_rate_metrics("All structures", all_flat)

    return {"structures": results, "totals_all": totals_all}
