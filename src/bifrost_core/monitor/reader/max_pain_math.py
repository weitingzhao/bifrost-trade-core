"""Max Pain math: EOD OI → pain(K) curve and argmin (no persistence)."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def normalize_expiry_for_oi(expiry: str) -> str:
    """Match option_open_interest_daily.expiry (YYYYMMDD)."""
    s = (expiry or "").strip()
    if len(s) >= 10 and s[4] == "-":
        return s[:4] + s[5:7] + s[8:10]
    return s.replace("-", "")


def strike_map_for_expiry(
    rows: List[Dict[str, Any]],
    target_expiry: str,
) -> Dict[float, Tuple[int, int]]:
    """Build strike -> (call_oi, put_oi) for one expiry from OI rows."""
    ne = normalize_expiry_for_oi(target_expiry)
    skmap: Dict[float, Tuple[int, int]] = {}
    for r in rows:
        exp = str(r.get("expiry") or "").strip()
        if normalize_expiry_for_oi(exp) != ne:
            continue
        try:
            sk = float(r.get("strike"))
        except (TypeError, ValueError):
            continue
        oi = int(r.get("open_interest") or 0)
        right = (r.get("option_right") or "").strip().upper()
        c_oi, p_oi = skmap.get(sk, (0, 0))
        if right == "C":
            skmap[sk] = (c_oi + oi, p_oi)
        elif right == "P":
            skmap[sk] = (c_oi, p_oi + oi)
    return skmap


def compute_max_pain_curve(skmap: Dict[float, Tuple[int, int]]) -> Tuple[float, float, List[Dict[str, Any]], int]:
    """Return (max_pain_strike, min_pain_value, points, total_oi).

    points: one entry per candidate strike K (sorted), with pain at K and OI at that strike.
    """
    if not skmap:
        return 0.0, 0.0, [], 0
    total_oi = sum(int(c) + int(p) for c, p in skmap.values())
    strikes_sorted = sorted(skmap.keys())
    points: List[Dict[str, Any]] = []
    best_x = strikes_sorted[0]
    best_pain: float | None = None
    for x in strikes_sorted:
        pain_call = 0.0
        pain_put = 0.0
        for s, (coi, poi) in skmap.items():
            pain_call += float(coi) * max(0.0, x - s) * 100.0
            pain_put += float(poi) * max(0.0, s - x) * 100.0
        pain = pain_call + pain_put
        c_at, p_at = skmap.get(x, (0, 0))
        points.append(
            {
                "strike": x,
                "pain": pain,
                "pain_call": pain_call,
                "pain_put": pain_put,
                "call_oi": int(c_at),
                "put_oi": int(p_at),
            }
        )
        if best_pain is None or pain < best_pain:
            best_pain = pain
            best_x = x
    return best_x, float(best_pain if best_pain is not None else 0.0), points, int(total_oi)
