"""Golden test for the Black-Scholes copies (TD-42): the dedup must not move a number.

Before TD-42 there were four Python copies:

* core ``pricing.black_scholes.delta`` / ``gamma`` -- py_vollib, used by the daemon's
  portfolio delta/gamma with ``greeks.risk_free_rate`` from config (0.05 in every env);
* core ``portfolio.model.black_scholes`` -- erf, the Positions model, r = 0.04;
* api ``research/routers/greeks.py`` -- erf, research IV & Greeks, r = 0.045 default;
* api ``research/routers/screener.py`` ``_prob_itm_put`` -- erf, r = 0.045.

The api copies and the pre-TD-42 core model are pasted verbatim under ``golden_oracles/``.
``fixtures/golden_black_scholes.json`` records what every copy returns over a grid
(S, K, T incl. T <= 0 and tiny T, sigma incl. 0 and tiny, every rate in use, C/P), plus
IV round trips, the IV acceptance edge cases and the right-string handling.

Three kinds of check:

1. core's current functions match the fixture (tolerance 1e-9 relative, so a libm with
   a different last bit does not fail it);
2. the oracles match the fixture (they are frozen copies, so this only guards the copy);
3. live, bit-for-bit: core's functions == the oracles on the same machine, with each
   caller's rate and conventions. This is the zero-diff proof.

Regenerate the fixture only on purpose:  PYTHONPATH=src python tests/test_golden_black_scholes.py
"""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List

import pytest

from golden_oracles import bs_api_greeks as api
from golden_oracles import bs_api_screener as screener
from golden_oracles import bs_core_model as model_oracle

from bifrost_core.portfolio.model import black_scholes as model
from bifrost_core.pricing import black_scholes as pricing

FIXTURE = Path(__file__).parent / "fixtures" / "golden_black_scholes.json"

S = 100.0
STRIKES = (70.0, 95.0, 100.0, 105.0, 140.0)
TIMES = (-1.0 / 365, 0.0, 1.0 / (365 * 24), 1.0 / 365, 30.0 / 365, 1.0)
SIGMAS = (0.0, 1e-4, 0.05, 0.30, 1.20)
# Every risk-free rate in use: 0 research GEX gamma (bifrost-research, r=q=0), 0.04 Positions
# model, 0.043 Symbol chain face (frontend), 0.045 research greeks + screener, 0.05 daemon.
RATES = (0.0, 0.04, 0.043, 0.045, 0.05)
RIGHTS = ("C", "P")

IV_PRICES = (-1.0, 0.0, 1e-4, 0.01, 0.5, 2.0, 5.0, 12.0, 31.0, 40.0, 120.0)
IV_STRIKES = (70.0, 100.0, 140.0)
IV_TIMES = (0.0, 7.0 / 365, 30.0 / 365, 1.0)
IV_RATES = (0.04, 0.045)

SCREENER_DTES = (-1, 0, 1, 7, 30, 365)

RIGHT_STRINGS = ("C", "P", "c", "p", "CALL", "PUT", "call", "put", "")
RIGHT_POINT = (100.0, 95.0, 30.0 / 365, 0.045, 0.30)

YEARS_TO_DAYS = (-5, 0, 1, 30, 365)


def _safe(fn: Callable[..., Any], *args: Any) -> Any:
    """The value, or {"error": <exception class>} -- the api copies raise on T <= 0."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - the class name is the recorded behaviour
        return {"error": type(exc).__name__}


def _num(v: Any) -> bool:
    return isinstance(v, float) and not isinstance(v, bool)


# --- what each copy returns ---------------------------------------------------------


def core_grid_case(K: float, T: float, sigma: float, r: float, right: str) -> Dict[str, Any]:
    """Core as it ships: daemon delta/gamma (py_vollib) and the Positions model (erf)."""
    out: Dict[str, Any] = {
        "vollib_delta": _safe(pricing.delta, S, K, T, r, sigma, right),
        "vollib_gamma": _safe(pricing.gamma, S, K, T, r, sigma, right),
        "model_price": _safe(model._bs_price, S, K, T, r, sigma, right),
        "model_delta": _safe(model._bs_delta, S, K, T, r, sigma, right),
    }
    mp = out["model_price"]
    out["model_iv"] = _safe(model._implied_vol, mp, S, K, T, r, right) if _num(mp) else None
    return out


def oracle_grid_case(K: float, T: float, sigma: float, r: float, right: str) -> Dict[str, Any]:
    """The api research copy (greeks.py) on the same point."""
    out: Dict[str, Any] = {
        "api_price": _safe(api._bs_price, S, K, T, r, sigma, right),
        "api_greeks": _safe(api.compute_greeks, S, K, T, r, sigma, right),
    }
    ap = out["api_price"]
    out["api_iv"] = _safe(api.implied_vol, ap, S, K, T, r, right) if _num(ap) else None
    return out


def grid_points() -> List[tuple]:
    return [
        (K, T, sigma, r, right)
        for K in STRIKES
        for T in TIMES
        for sigma in SIGMAS
        for r in RATES
        for right in RIGHTS
    ]


def iv_points() -> List[tuple]:
    return [
        (p, K, T, r, right)
        for p in IV_PRICES
        for K in IV_STRIKES
        for T in IV_TIMES
        for r in IV_RATES
        for right in RIGHTS
    ]


def core_iv_case(p: float, K: float, T: float, r: float, right: str) -> Any:
    return _safe(model._implied_vol, p, S, K, T, r, right)


def oracle_iv_case(p: float, K: float, T: float, r: float, right: str) -> Any:
    return _safe(api.implied_vol, p, S, K, T, r, right)


def screener_points() -> List[tuple]:
    return [(K, dte, iv) for K in STRIKES for dte in SCREENER_DTES for iv in SIGMAS]


def oracle_screener_case(K: float, dte: int, iv: float) -> Any:
    return _safe(screener._prob_itm_put, S, K, dte, iv)


def core_rights_case(right: str) -> Dict[str, Any]:
    s, k, t, r, sig = RIGHT_POINT
    return {
        "vollib_delta": _safe(pricing.delta, s, k, t, r, sig, right),
        "vollib_gamma": _safe(pricing.gamma, s, k, t, r, sig, right),
        "model_price": _safe(model._bs_price, s, k, t, r, sig, right),
        "model_delta": _safe(model._bs_delta, s, k, t, r, sig, right),
        "model_iv": _safe(model._implied_vol, 3.0, s, k, t, r, right),
    }


def oracle_rights_case(right: str) -> Dict[str, Any]:
    s, k, t, r, sig = RIGHT_POINT
    return {
        "api_price": _safe(api._bs_price, s, k, t, r, sig, right),
        "api_greeks": _safe(api.compute_greeks, s, k, t, r, sig, right),
        "api_iv": _safe(api.implied_vol, 3.0, s, k, t, r, right),
    }


def core_years_to() -> Dict[str, Any]:
    today = date.today()
    out: Dict[str, Any] = {"None": model._years_to(None)}
    for d in YEARS_TO_DAYS:
        out[str(d)] = model._years_to(today + timedelta(days=d))
    return out


def _columns(rows: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    """List of per-point dicts -> one list per output (keeps the fixture small)."""
    return {k: [row[k] for row in rows] for k in rows[0]}


def build_core() -> Dict[str, Any]:
    return {
        "grid": _columns([core_grid_case(*pt) for pt in grid_points()]),
        "iv": [core_iv_case(*pt) for pt in iv_points()],
        "rights": {rt: core_rights_case(rt) for rt in RIGHT_STRINGS},
        "years_to": core_years_to(),
    }


def build_oracle() -> Dict[str, Any]:
    return {
        "grid": _columns([oracle_grid_case(*pt) for pt in grid_points()]),
        "iv": [oracle_iv_case(*pt) for pt in iv_points()],
        "screener": [oracle_screener_case(*pt) for pt in screener_points()],
        "rights": {rt: oracle_rights_case(rt) for rt in RIGHT_STRINGS},
    }


def _trim(v: Any) -> Any:
    """13 significant digits are enough for a 1e-9 comparison and keep the file small."""
    if isinstance(v, float) and math.isfinite(v):
        return float(f"{v:.13g}")
    if isinstance(v, dict):
        return {k: _trim(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_trim(x) for x in v]
    return v


def build() -> Dict[str, Any]:
    """The fixture. Point i of each list is grid_points()[i] / iv_points()[i] / ..."""
    return _trim(
        {
            "about": "TD-42 golden values; see tests/test_golden_black_scholes.py",
            "core": build_core(),
            "oracle": build_oracle(),
        }
    )


# --- comparison -----------------------------------------------------------------------


def _close(a: Any, b: Any, path: str, bad: List[str], *, rel: float = 1e-9) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            bad.append(f"{path}: keys {sorted(a)} != {sorted(b)}")
            return
        for k in a:
            _close(a[k], b[k], f"{path}.{k}", bad, rel=rel)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            bad.append(f"{path}: len {len(a)} != {len(b)}")
            return
        for i, (x, y) in enumerate(zip(a, b)):
            _close(x, y, f"{path}[{i}]", bad, rel=rel)
    elif isinstance(a, (int, float)) and isinstance(b, (int, float)) and not (
        isinstance(a, bool) or isinstance(b, bool)
    ):
        if math.isnan(a) and math.isnan(b):
            return
        if not math.isclose(a, b, rel_tol=rel, abs_tol=1e-12):
            bad.append(f"{path}: {a!r} != {b!r}")
    elif a != b:
        bad.append(f"{path}: {a!r} != {b!r}")


@pytest.fixture(scope="module")
def golden() -> Dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _assert_close(actual: Any, expected: Any, path: str) -> None:
    bad: List[str] = []
    _close(actual, expected, path, bad)
    assert not bad, f"{len(bad)} differences, first: {bad[:5]}"


def test_fixture_covers_the_grid(golden: Dict[str, Any]) -> None:
    assert len(golden["core"]["grid"]["model_price"]) == len(grid_points()) == 1500
    assert len(golden["oracle"]["grid"]["api_price"]) == len(grid_points())
    assert len(golden["core"]["iv"]) == len(golden["oracle"]["iv"]) == len(iv_points())
    assert len(golden["oracle"]["screener"]) == len(screener_points())
    # The grid reaches every branch: expiry and past expiry, zero vol, every rate in use.
    assert {p[1] for p in grid_points()} >= {0.0, -1.0 / 365}
    assert {p[2] for p in grid_points()} >= {0.0}
    assert {p[3] for p in grid_points()} == set(RATES)
    # ... and the api copy really raises there, which core must reproduce.
    assert any(isinstance(g, dict) for g in golden["oracle"]["grid"]["api_price"])


def test_core_matches_the_fixture(golden: Dict[str, Any]) -> None:
    _assert_close(build_core(), golden["core"], "core")


def test_oracles_match_the_fixture(golden: Dict[str, Any]) -> None:
    _assert_close(build_oracle(), golden["oracle"], "oracle")


# --- live, bit for bit -----------------------------------------------------------------


def _same(a: Any, b: Any) -> bool:
    """Exact: same float bits (NaN == NaN), same None, same exception class."""
    if isinstance(a, float) and isinstance(b, float):
        return (math.isnan(a) and math.isnan(b)) or a == b
    return a == b


def _exact(pairs: List[tuple]) -> None:
    bad = [(label, a, b) for label, a, b in pairs if not _same(a, b)]
    assert not bad, f"{len(bad)} of {len(pairs)} differ, first: {bad[:5]}"


def test_core_model_is_bit_identical_to_its_pre_td42_copy() -> None:
    pairs: List[tuple] = []
    for K, T, sigma, r, right in grid_points():
        args = (S, K, T, r, sigma, right)
        p = _safe(model._bs_price, *args)
        pairs.append((("price",) + args, p, _safe(model_oracle._bs_price, *args)))
        pairs.append((("delta",) + args, _safe(model._bs_delta, *args), _safe(model_oracle._bs_delta, *args)))
        if _num(p):
            iv_args = (p, S, K, T, r, right)
            pairs.append((("iv",) + iv_args, _safe(model._implied_vol, *iv_args), _safe(model_oracle._implied_vol, *iv_args)))
    for p, K, T, r, right in iv_points():
        iv_args = (p, S, K, T, r, right)
        pairs.append((("iv",) + iv_args, _safe(model._implied_vol, *iv_args), _safe(model_oracle._implied_vol, *iv_args)))
    for right in RIGHT_STRINGS:
        s, k, t, r, sig = RIGHT_POINT
        for fn in ("_bs_price", "_bs_delta"):
            pairs.append(((fn, right), _safe(getattr(model, fn), s, k, t, r, sig, right), _safe(getattr(model_oracle, fn), s, k, t, r, sig, right)))
    today = date.today()
    for d in YEARS_TO_DAYS:
        exp = today + timedelta(days=d)
        pairs.append((("years_to", d), model._years_to(exp), model_oracle._years_to(exp)))
    _exact(pairs)


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    FIXTURE.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
