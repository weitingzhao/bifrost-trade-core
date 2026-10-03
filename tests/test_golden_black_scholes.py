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


# An implied vol is only defined up to the solver's stopping rule, and not at all where the
# price is flat in sigma (at intrinsic, or ~0). There a last-bit libm difference (macOS vs the
# Linux CI) moves the solver to a different, equally valid sigma -- or across its None/value
# acceptance edge. So across machines IVs are compared by what they price, not by their digits.
# The same-machine tests below stay bit-for-bit.
#
# Each solver stops once |price - market| < its tol (core model 1e-6, api copy 1e-8), so two
# valid answers reprice up to 2 x tol apart.
_REPRICE_TOL = {"model_iv": 2e-6, "api_iv": 2e-8}


def _reprice(sigma: float, S_: float, K: float, T: float, r: float, right: str) -> float:
    return model._bs_price(S_, K, T, r, sigma, right)


def _flat_in_sigma(sigma: float, S_: float, K: float, T: float, r: float, right: str, tol: float) -> bool:
    """Halving sigma does not move the price: no IV is identifiable at this point."""
    return abs(_reprice(sigma, S_, K, T, r, right) - _reprice(sigma / 2, S_, K, T, r, right)) <= tol


def _iv_equivalent(a: Any, b: Any, K: float, T: float, r: float, right: str, tol: float) -> bool:
    if _same(a, b) or a == b:
        return True
    if _num(a) and _num(b):
        if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12):
            return True
        return abs(_reprice(a, S, K, T, r, right) - _reprice(b, S, K, T, r, right)) <= tol
    if a is None and _num(b):
        return _flat_in_sigma(b, S, K, T, r, right, tol)
    if b is None and _num(a):
        return _flat_in_sigma(a, S, K, T, r, right, tol)
    return False


def _split_ivs(actual: Dict[str, Any], expected: Dict[str, Any], grid_key: str) -> List[str]:
    """Check the grid and iv-list IVs by price; drop them so _close sees everything else."""
    bad: List[str] = []
    tol = _REPRICE_TOL[grid_key]
    for i, (K, T, _sigma, r, right) in enumerate(grid_points()):
        a, b = actual["grid"][grid_key][i], expected["grid"][grid_key][i]
        if not _iv_equivalent(a, b, K, T, r, right, tol):
            bad.append(f"grid.{grid_key}[{i}]: {a!r} != {b!r}")
    for i, (_p, K, T, r, right) in enumerate(iv_points()):
        a, b = actual["iv"][i], expected["iv"][i]
        if not _iv_equivalent(a, b, K, T, r, right, tol):
            bad.append(f"iv[{i}]: {a!r} != {b!r}")
    for d in (actual, expected):
        d["grid"] = {k: v for k, v in d["grid"].items() if k != grid_key}
        d.pop("iv")
    return bad


def _assert_matches_fixture(actual: Dict[str, Any], expected: Dict[str, Any], path: str, grid_key: str) -> None:
    expected = json.loads(json.dumps(expected))
    bad = [f"{path}.{x}" for x in _split_ivs(actual, expected, grid_key)]
    _close(actual, expected, path, bad)
    assert not bad, f"{len(bad)} differences, first: {bad[:5]}"


def test_core_matches_the_fixture(golden: Dict[str, Any]) -> None:
    _assert_matches_fixture(build_core(), golden["core"], "core", "model_iv")


def test_oracles_match_the_fixture(golden: Dict[str, Any]) -> None:
    _assert_matches_fixture(build_oracle(), golden["oracle"], "oracle", "api_iv")


def test_iv_comparison_still_catches_a_real_change() -> None:
    """The price-space rule must not wave through a moved IV at a well-conditioned point."""
    K, T, r, right = 100.0, 30.0 / 365, 0.045, "C"
    for tol in _REPRICE_TOL.values():
        assert _iv_equivalent(0.30, 0.30 * (1 + 1e-12), K, T, r, right, tol)
        assert not _iv_equivalent(0.30, 0.3001, K, T, r, right, tol)
        assert not _iv_equivalent(0.30, 0.300001, K, T, r, right, tol)
        assert not _iv_equivalent(None, 0.30, K, T, r, right, tol)
        # Far OTM put with no time value: every small sigma prices ~0, so any answer is one.
        assert _iv_equivalent(None, 0.04, 70.0, 1.0, 0.05, "P", tol)


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


# --- core's public functions reproduce the api copies (TD-42) --------------------------------
#
# How the api calls core when it switches: its own right rule mapped to "C"/"P", strict=True
# (the raw formula raises where the api copy raised), IV_RESEARCH, RATE_RESEARCH.


def _api_right(right: str) -> str:
    return "C" if right.upper() == "C" else "P"


def core_as_api_price(S_: float, K: float, T: float, r: float, sigma: float, right: str) -> float:
    return pricing.price(S_, K, T, r, sigma, _api_right(right), strict=True)


def core_as_api_greeks(S_: float, K: float, T: float, r: float, sigma: float, right: str) -> Dict[str, float]:
    g = pricing.greeks(S_, K, T, r, sigma, _api_right(right), strict=True)
    return {k: round(v, 6) for k, v in g.items()}


def core_as_api_iv(p: float, S_: float, K: float, T: float, r: float, right: str) -> Any:
    return pricing.implied_vol(p, S_, K, T, r, _api_right(right), convention=pricing.IV_RESEARCH)


def core_as_screener(spot: float, strike: float, dte: int, iv: float) -> float:
    return pricing.prob_itm(spot, strike, dte / 365.0, pricing.RATE_RESEARCH, iv, "P")


def test_core_reproduces_the_api_research_greeks_bit_for_bit(monkeypatch: pytest.MonkeyPatch) -> None:
    pairs: List[tuple] = []
    for K, T, sigma, r, right in grid_points():
        args = (S, K, T, r, sigma, right)
        ap = _safe(api._bs_price, *args)
        pairs.append((("price",) + args, _safe(core_as_api_price, *args), ap))
        pairs.append((("greeks",) + args, _safe(core_as_api_greeks, *args), _safe(api.compute_greeks, *args)))
        pairs.append((("vega",) + args, _safe(lambda *a: pricing.vega(*a, strict=True), S, K, T, r, sigma),
                      _safe(api._bs_vega, S, K, T, r, sigma)))
        pairs.append((("d1d2",) + args,
                      _safe(lambda *a: (pricing.d1(*a, strict=True), pricing.d2(*a, strict=True)), S, K, T, r, sigma),
                      _safe(api._bs_d1d2, S, K, T, r, sigma)))
        if _num(ap):
            iv_args = (ap, S, K, T, r, right)
            pairs.append((("iv",) + iv_args, _safe(core_as_api_iv, *iv_args), _safe(api.implied_vol, *iv_args)))
    for p, K, T, r, right in iv_points():
        iv_args = (p, S, K, T, r, right)
        pairs.append((("iv",) + iv_args, _safe(core_as_api_iv, *iv_args), _safe(api.implied_vol, *iv_args)))
    for right in RIGHT_STRINGS:
        s, k, t, r, sig = RIGHT_POINT
        pairs.append((("price", right), _safe(core_as_api_price, s, k, t, r, sig, right), _safe(api._bs_price, s, k, t, r, sig, right)))
        pairs.append((("greeks", right), _safe(core_as_api_greeks, s, k, t, r, sig, right), _safe(api.compute_greeks, s, k, t, r, sig, right)))
        pairs.append((("iv", right), _safe(core_as_api_iv, 3.0, s, k, t, r, right), _safe(api.implied_vol, 3.0, s, k, t, r, right)))
    # Unrounded too: shadow round() in the oracle's module so compute_greeks returns raw floats.
    monkeypatch.setattr(api, "round", lambda x, _n: x, raising=False)
    for K, T, sigma, r, right in grid_points():
        args = (S, K, T, r, sigma, right)
        raw = _safe(lambda *a: pricing.greeks(*a[:5], _api_right(a[5]), strict=True), *args)
        pairs.append((("raw greeks",) + args, raw, _safe(api.compute_greeks, *args)))
    _exact(pairs)


def test_core_reproduces_the_screener_bit_for_bit() -> None:
    pairs = [
        (pt, _safe(core_as_screener, S, *pt), _safe(screener._prob_itm_put, S, *pt))
        for pt in screener_points()
    ]
    _exact(pairs)


def test_core_public_model_conventions_match_the_model_copy() -> None:
    """The model's wrappers aside: the public functions with right "C"/"P" are the model."""
    pairs: List[tuple] = []
    for K, T, sigma, r, right in grid_points():
        args = (S, K, T, r, sigma, right)
        pairs.append((args, _safe(pricing.price, *args), _safe(model_oracle._bs_price, *args)))
        pairs.append((args, _safe(pricing.erf_delta, *args), _safe(model_oracle._bs_delta, *args)))
    for p, K, T, r, right in iv_points():
        iv_args = (p, S, K, T, r, right)
        pairs.append((iv_args, _safe(lambda *a: pricing.implied_vol(*a, convention=pricing.IV_POSITIONS_MODEL), *iv_args),
                      _safe(model_oracle._implied_vol, *iv_args)))
    _exact(pairs)


def test_calculate_greeks_is_the_daemon_delta_gamma_plus_erf_theta_vega(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "round", lambda x, _n: x, raising=False)
    pairs: List[tuple] = []
    for K, T, sigma, r, right in grid_points():
        got = pricing.calculate_greeks(S, K, T, r, sigma, right)
        pairs.append((("delta", K, T, sigma, r, right), got["delta"], pricing.delta(S, K, T, r, sigma, right)))
        pairs.append((("gamma", K, T, sigma, r, right), got["gamma"], pricing.gamma(S, K, T, r, sigma, right)))
        if T <= 0 or sigma <= 0:
            want = {"theta": 0.0, "vega": 0.0}
        else:
            want = api.compute_greeks(S, K, T, r, sigma, right)
        pairs.append((("theta", K, T, sigma, r, right), got["theta"], want["theta"]))
        pairs.append((("vega", K, T, sigma, r, right), got["vega"], want["vega"]))
    _exact(pairs)


def test_each_surface_keeps_its_own_rate() -> None:
    """Named, not unified: changing one of these moves that surface's numbers."""
    import inspect

    from bifrost_core.portfolio.model import core as model_core

    assert pricing.RATE_RESEARCH_GEX == 0.0
    assert pricing.RATE_POSITIONS_MODEL == 0.04
    assert pricing.RATE_SYMBOL_CHAIN_UI == 0.043
    assert pricing.RATE_RESEARCH == 0.045 == api.DEFAULT_RISK_FREE_RATE == screener.RISK_FREE_RATE
    assert pricing.RATE_DAEMON_DEFAULT == 0.05
    for fn in (model_core._compute_greeks_for_group, model_core._stress_matrix):
        assert inspect.signature(fn).parameters["r"].default == 0.04


def test_implied_vol_needs_a_known_convention() -> None:
    with pytest.raises(ValueError):
        pricing.implied_vol(2.0, 100.0, 100.0, 0.1, 0.04, "C", convention="unified")


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    data = build()
    FIXTURE.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")
    print(f"wrote {FIXTURE} ({FIXTURE.stat().st_size} bytes)")
