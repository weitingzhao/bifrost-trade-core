"""contract_key: the one place core spells a key (TD-25).

Every format here is persisted (brokerage positions and executions, watchlist, journal
copies) or joined on, so a builder may never change its output: the golden test
(tests/test_golden_contract_key.py) holds each one byte for byte against the inline code it
replaced. New code builds keys here instead of with an f-string.

Formats in use -- they differ, and that is history, not a choice to repeat:

* stock and other non-option rows: ``SYMBOL|SEC|||`` -- ``stk_key``.
* positions (IB snapshot): ``SYMBOL|OPT|EXPIRY|STRIKE|RIGHT`` with the strike as Python
  prints a float (``80.0``, ``82.5``) and the text ``None`` when there is no strike; symbol,
  expiry and right as IB sent them -- ``opt_key(..., none_text="None")``.
* executions from TWS (``tws_event`` / ``tws_client``): the symbol slot holds a local symbol
  ``SYMBOL  YYMMDDR########`` -- two spaces whatever the root length, which is *not* the
  OSI/OCC layout -- then ``|OPT|YYYYMMDD|80.0|C`` -- ``tws_execution_opt_key``.
* other executions: ``SYMBOL|OPT|YYYYMMDD|80.0|C`` with an empty strike when there is none.
* the join from positions to executions tries OSI local symbols (6-char padded root) --
  ``osi_local_symbol``.
* rows read without a key get one at read time (never written back) -- ``read_fallback_opt_key``.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Optional, Tuple

TWS_SOURCES = ("tws_event", "tws_client")


def stk_key(symbol: Any, sec_type: Any = "STK") -> str:
    """``SYMBOL|STK|||`` (or ``SYMBOL|<sec_type>|||`` for any non-option row)."""
    return f"{symbol}|{sec_type}|||"


def opt_key(symbol: Any, expiry: Any, strike: Any, right: Any, *, none_text: str = "") -> str:
    """``SYMBOL|OPT|EXPIRY|STRIKE|RIGHT``. Each part is printed as given (a float strike 80.0
    is ``80.0``); a None strike is ``none_text`` -- ``""`` for executions, ``"None"`` for the
    positions sync, which has always written that text."""
    return f"{symbol}|OPT|{expiry}|{none_text if strike is None else strike}|{right}"


def execution_opt_fields(
    symbol: Any, expiry: Any, strike: Any, option_right: Any
) -> Tuple[str, str, Optional[float], str]:
    """How the execution writers normalise an option row: stripped symbol, expiry digits
    without dashes (an int/float expiry printed as an int), strike as a float or None, and the
    right cut to one letter (``CALL`` -> ``C``)."""
    sym_key = (symbol or "").strip()
    exp_val = expiry
    if isinstance(exp_val, (int, float)) and math.isfinite(exp_val):
        exp_key = str(int(exp_val))
    else:
        exp_key = (exp_val or "").strip().replace("-", "")
    try:
        strike_key = float(strike) if strike not in ("", None) else None
    except (TypeError, ValueError):
        strike_key = None
    right_key = (option_right or "").strip().upper()
    if len(right_key) > 1:
        right_key = "C" if right_key.startswith("C") else "P" if right_key.startswith("P") else right_key[:1]
    return sym_key, exp_key, strike_key, right_key


def legacy_tws_local_symbol(sym_key: str, exp_key: str, strike_key: float, right_key: str) -> Optional[str]:
    """The local symbol TWS execution keys have always carried: ``SYMBOL  YYMMDD`` + right +
    strike x 1000 in 8 digits, with exactly two spaces after the symbol.

    Legacy, kept as is: IB/OSI pads the root to 6 characters (``osi_local_symbol``), this
    does not, and executions are stored with this form. None when the expiry has no digits or
    the strike cannot be scaled."""
    exp_digits = "".join(ch for ch in exp_key if ch.isdigit())
    yymmdd = exp_digits[2:8] if len(exp_digits) >= 8 else exp_digits[-6:]
    try:
        strike_int = int(round(strike_key * 1000.0))
    except (TypeError, ValueError, OverflowError):
        return None
    if not yymmdd:
        return None
    return f"{sym_key}  {yymmdd}{right_key}{strike_int:08d}"


def tws_execution_opt_key(symbol: Any, expiry: Any, strike: Any, option_right: Any) -> Optional[str]:
    """The key a TWS option execution is stored under, or None when a part is missing (the
    writer then keeps whatever key it had)."""
    sym_key, exp_key, strike_key, right_key = execution_opt_fields(symbol, expiry, strike, option_right)
    if not (sym_key and exp_key and strike_key is not None and right_key):
        return None
    local = legacy_tws_local_symbol(sym_key, exp_key, strike_key, right_key)
    if local is None:
        return None
    return opt_key(local, exp_key, strike_key, right_key)


def osi_local_symbol(symbol: str, expiry_yyyymmdd: str, strike: float, right: str) -> str:
    """IB/OCC-style local symbol root: 6-char root (space-pad) + YYMMDD + C/P + strike*1000 (8 digits)."""
    exp = re.sub(r"\D", "", expiry_yyyymmdd or "")
    if len(exp) >= 8:
        yymmdd = exp[2:8]
    elif len(exp) == 6:
        yymmdd = exp
    else:
        yymmdd = (exp + "010101")[:6]
    root = (symbol or "").strip().upper()[:6].ljust(6)
    r = (right or "C").strip().upper()[:1]
    if r not in ("C", "P"):
        r = "C"
    strike_milli = int(round(float(strike) * 1000))
    strike_milli = max(0, min(strike_milli, 99999999))
    return f"{root}{yymmdd}{r}{strike_milli:08d}"


def _fallback_strike_text(strike: Any) -> str:
    """Integral strikes print without a decimal (``80``), as this fallback always did;
    fractional ones print in full (``82.5``). Until core 0.34.0 every strike went through
    ``int()``, so 82.5 became ``82`` and collided with the 82 strike (TD-25)."""
    if not math.isfinite(strike):
        return ""
    return str(int(strike)) if strike == int(strike) else str(float(strike))


def read_fallback_opt_key(row: Mapping[str, Any]) -> Optional[str]:
    """The key given at read time to an OPT row stored without one, or None when the row is not
    an option or already has a key. Never persisted.

    ``SYMBOL|OPT|YYYYMMDD|STRIKE|R`` from symbol, expiry, strike and option_right (or right)."""
    if (row.get("sec_type") or "").strip().upper() != "OPT":
        return None
    if (row.get("contract_key") or "").strip():
        return None
    sym = (row.get("symbol") or "").strip()
    exp = row.get("expiry") or ""
    if isinstance(exp, (int, float)) and math.isfinite(exp):
        exp = str(int(exp))
    else:
        exp = (exp or "").strip().replace("-", "")
    strike = row.get("strike")
    if strike is not None and not isinstance(strike, str):
        strike = _fallback_strike_text(strike)
    else:
        strike = (strike or "").strip()
    right = (row.get("option_right") or "").strip().upper()
    if len(right) > 1:
        right = "C" if right.startswith("C") else "P" if right.startswith("P") else right[:1]
    if not right and "right" in row:
        right = (row.get("right") or "").strip().upper()[:1] or ""
    return opt_key(sym, exp, strike, right)
