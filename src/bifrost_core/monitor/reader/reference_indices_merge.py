"""Merge ``reference_indices`` from PostgreSQL control and merged YAML; augment from DB caret symbols."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from bifrost_core.monitor.reader.symbol_normalize import norm_bars_symbol


def merge_reference_indices(
    db_list: Optional[List[Any]],
    file_list: Optional[List[Any]],
) -> List[Dict[str, Any]]:
    """Union by normalized ``symbol``; fields from *file_list* win on collision. Order: *file_list* order, then DB-only symbols."""
    merged: Dict[str, Dict[str, Any]] = {}
    for item in db_list or []:
        if not isinstance(item, dict):
            continue
        s = (item.get("symbol") or "").strip()
        if s:
            merged[norm_bars_symbol(s)] = dict(item)
    for item in file_list or []:
        if not isinstance(item, dict):
            continue
        s = (item.get("symbol") or "").strip()
        if s:
            merged[norm_bars_symbol(s)] = dict(item)
    ordered: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for item in file_list or []:
        if not isinstance(item, dict):
            continue
        s = (item.get("symbol") or "").strip()
        if s:
            nk = norm_bars_symbol(s)
            if nk not in seen:
                seen.add(nk)
                ordered.append(merged[nk])
    for item in db_list or []:
        if not isinstance(item, dict):
            continue
        s = (item.get("symbol") or "").strip()
        if s:
            nk = norm_bars_symbol(s)
            if nk not in seen:
                seen.add(nk)
                ordered.append(merged[nk])
    return ordered


def augment_reference_indices_with_caret_symbols(
    reference_indices: List[Dict[str, Any]],
    caret_symbols: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """Append minimal ``{symbol, label}`` rows for ``^...`` symbols present in bars tables but missing from *reference_indices*."""
    if not caret_symbols:
        return reference_indices
    have = {norm_bars_symbol(r.get("symbol") or "") for r in reference_indices if isinstance(r, dict)}
    extra: List[Dict[str, Any]] = []
    for s in caret_symbols:
        sym = (s or "").strip()
        nk = norm_bars_symbol(sym)
        if nk.startswith("^") and nk not in have:
            have.add(nk)
            extra.append({"symbol": sym, "label": sym})
    if not extra:
        return reference_indices
    return list(reference_indices) + extra
