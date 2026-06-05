"""Execution-book Net PnL per strategy instance (matches Instance Detail / frontend computeInstanceExecDerivedNetPnl)."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from bifrost_core.portfolio.reader.executions import get_executions
from bifrost_core.portfolio.reader.option_stock_link import get_option_stock_links_bulk

logger = logging.getLogger(__name__)

NET_QTY_EPS = 1e-9


def _f(x: Any, default: float = 0.0) -> float:
    if x is None:
        return default
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def slice_execution_for_instance_opt_view(ex: Dict[str, Any], instance_id: int) -> Optional[Dict[str, Any]]:
    """Mirror of frontend sliceExecutionForInstanceOptView (allocated qty + prorated realized/commission)."""
    try:
        sid = int(instance_id)
    except (TypeError, ValueError):
        return None
    allocs = ex.get("instance_allocations") or []
    if allocs:
        denom = 0.0
        for a in allocs:
            denom += abs(_f(a.get("allocated_quantity"), 0.0))
        if denom <= NET_QTY_EPS:
            return None
        mine = None
        for a in allocs:
            try:
                if int(a.get("strategy_instance_id")) == sid:
                    mine = a
                    break
            except (TypeError, ValueError):
                continue
        if mine is None:
            return None
        alloc_qty = _f(mine.get("allocated_quantity"), 0.0)
        if not math.isfinite(alloc_qty):
            return None
        w = abs(alloc_qty) / denom
        out = dict(ex)
        out["quantity"] = alloc_qty
        rp = ex.get("realized_pnl")
        if rp is not None:
            rv = _f(rp, 0.0)
            if math.isfinite(rv):
                out["realized_pnl"] = rv * w
        comm = ex.get("commission")
        if comm is not None:
            cv = _f(comm, 0.0)
            if math.isfinite(cv):
                out["commission"] = cv * w
        taxes = ex.get("taxes")
        if taxes is not None:
            tv = _f(taxes, 0.0)
            if math.isfinite(tv):
                out["taxes"] = tv * w
        nc = ex.get("net_cash")
        if nc is not None:
            nv = _f(nc, 0.0)
            if math.isfinite(nv):
                out["net_cash"] = nv * w
        alloc_opp = mine.get("strategy_opportunity_id")
        parent_opp = ex.get("strategy_opportunity_id")
        resolved_opp = None
        if alloc_opp is not None:
            try:
                resolved_opp = int(alloc_opp)
            except (TypeError, ValueError):
                resolved_opp = None
        if resolved_opp is None and parent_opp is not None:
            try:
                resolved_opp = int(parent_opp)
            except (TypeError, ValueError):
                resolved_opp = None
        out["strategy_opportunity_id"] = resolved_opp
        if resolved_opp is not None and parent_opp is not None:
            try:
                if int(resolved_opp) == int(parent_opp):
                    nm = ex.get("strategy_opportunity_name")
                    if nm is not None and str(nm).strip():
                        out["strategy_opportunity_name"] = str(nm).strip()
            except (TypeError, ValueError):
                pass
        else:
            out["strategy_opportunity_name"] = None
        out["strategy_instance_id"] = sid
        lbl = mine.get("strategy_instance_label")
        if lbl is not None and str(lbl).strip():
            out["strategy_instance_label"] = str(lbl).strip()
        out["instance_allocations"] = None
        return out
    si = ex.get("strategy_instance_id")
    if si is not None:
        try:
            if int(si) == sid:
                return dict(ex)
        except (TypeError, ValueError):
            pass
    return None


def _opt_group_key(ex: Dict[str, Any]) -> str:
    """Match frontend buildOptExecutionGroups: contract_key + numeric strike (avoid Decimal/str splits)."""
    ck = (ex.get("contract_key") or "").strip()
    sk = ex.get("strike")
    try:
        sn = float(sk) if sk is not None and sk != "" else 0.0
    except (TypeError, ValueError):
        sn = 0.0
    if not math.isfinite(sn):
        sn = 0.0
    return f"{ck}|{sn}"


def _sum_opt_groups_realized_pnl(sliced: List[Dict[str, Any]]) -> float:
    """Same algebra as frontend buildOptExecutionGroups realized_pnl per contract key."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in sliced:
        if (e.get("sec_type") or "").upper() != "OPT":
            continue
        groups[_opt_group_key(e)].append(e)
    total = 0.0
    for trades in groups.values():
        buy_value = 0.0
        sell_value = 0.0
        for t in trades:
            q = abs(_f(t.get("quantity")))
            if q < NET_QTY_EPS:
                continue
            p = _f(t.get("price"))
            c = _f(t.get("commission"))
            side = (t.get("side") or "").upper()
            if side in ("BUY", "BOT", "B"):
                buy_value += p * q * 100 + c
            elif side in ("SELL", "SLD", "S"):
                sell_value += p * q * 100 - c
        total += sell_value - buy_value
    return total


def _sum_non_opt_realized_pnl(sliced: List[Dict[str, Any]]) -> float:
    s = 0.0
    for e in sliced:
        if (e.get("sec_type") or "").upper() == "OPT":
            continue
        rp = e.get("realized_pnl")
        if rp is None:
            continue
        v = _f(rp, float("nan"))
        if math.isfinite(v):
            s += v
    return s


def _instance_option_stock_slippage_adjustment(
    conn: Any,
    raw_executions: List[Dict[str, Any]],
    instance_id: int,
) -> float:
    """Prorated option–stock link slippage (same as frontend instanceOptionStockSlippageAdjustment)."""
    by_account: Dict[str, List[int]] = defaultdict(list)
    seen_per_acc: Dict[str, set] = defaultdict(set)
    for ex in raw_executions:
        if (ex.get("sec_type") or "").upper() != "OPT":
            continue
        if slice_execution_for_instance_opt_view(ex, instance_id) is None:
            continue
        oid = ex.get("account_executions_id")
        if oid is None:
            continue
        try:
            oi = int(oid)
        except (TypeError, ValueError):
            continue
        acc = (ex.get("account_id") or "").strip()
        if not acc:
            continue
        if oi in seen_per_acc[acc]:
            continue
        seen_per_acc[acc].add(oi)
        by_account[acc].append(oi)
    batches: List[Tuple[str, List[int]]] = [(acc, ids) for acc, ids in by_account.items() if ids]
    if not batches:
        return 0.0
    try:
        resp = get_option_stock_links_bulk(conn, batches)
    except Exception as ex:
        logger.warning("instance_exec_net_pnl: get_option_stock_links_bulk failed: %s", ex)
        return 0.0
    by_oid = resp.get("by_option_id") or {}
    total_slip = 0.0
    for ex in raw_executions:
        if (ex.get("sec_type") or "").upper() != "OPT":
            continue
        sl = slice_execution_for_instance_opt_view(ex, instance_id)
        if sl is None:
            continue
        oid = ex.get("account_executions_id")
        if oid is None:
            continue
        try:
            oi = int(oid)
        except (TypeError, ValueError):
            continue
        parent_qty = abs(_f(ex.get("quantity")))
        if parent_qty < NET_QTY_EPS:
            continue
        slice_qty = abs(_f(sl.get("quantity")))
        ratio = slice_qty / parent_qty
        entry = by_oid.get(str(oi))
        if not entry:
            continue
        slip = _slippage_usd_from_link_entry(entry)
        total_slip += slip * ratio
    return total_slip


def _slippage_usd_from_link_entry(entry: Dict[str, Any]) -> float:
    """Prefer slippage_total; if missing, sum link slippage_vs_close (parity with frontend OptionStockLinkSummary)."""
    st = entry.get("slippage_total")
    if st is not None:
        v = _f(st, float("nan"))
        if math.isfinite(v):
            return v
    total = 0.0
    for d in entry.get("links") or []:
        s = d.get("slippage_vs_close")
        if s is None:
            continue
        fv = _f(s, float("nan"))
        if math.isfinite(fv):
            total += fv
    return total


def compute_instance_exec_derived_net_pnl(
    conn: Any,
    strategy_instance_id: int,
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
) -> float:
    """
    OPT: premium ± commission groups; non-OPT: DB realized_pnl; plus prorated option–stock slippage.
    Same window and book as GET /executions?source_scope=performance_book&strategy_instance_id=…
    """
    if conn is None:
        return 0.0
    try:
        sid = int(strategy_instance_id)
    except (TypeError, ValueError):
        return 0.0
    raw = get_executions(
        conn,
        since_ts=since_ts,
        until_ts=until_ts,
        account_id=None,
        limit=50000,
        strategy_opportunity_id=None,
        strategy_instance_id=sid,
        source_scope="performance_book",
    )
    sliced: List[Dict[str, Any]] = []
    for ex in raw:
        sl = slice_execution_for_instance_opt_view(ex, sid)
        if sl is not None:
            sliced.append(sl)
    if not sliced:
        return 0.0
    opt_part = _sum_opt_groups_realized_pnl(sliced)
    non_opt_part = _sum_non_opt_realized_pnl(sliced)
    slip = _instance_option_stock_slippage_adjustment(conn, raw, sid)
    total = opt_part + non_opt_part + slip
    if not math.isfinite(total):
        return 0.0
    return round(total, 2)
