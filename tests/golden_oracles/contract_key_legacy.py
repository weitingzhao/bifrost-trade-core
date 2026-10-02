"""Legacy oracle: every contract_key builder in core before TD-25 (core 0.33.2, 84a63fb).

The statements are copied verbatim; the inline ones are wrapped in a function whose
arguments are the variables the original block read. Do not edit (TD-25 golden test).

* positions_key        -- persistence/postgres/accounts_sync.py:140-170 (brokerage positions)
* sink_execution_key   -- persistence/postgres/postgres_sink.py:351-403 (daemon executions)
* accounts_execution_key -- portfolio/reader/accounts.py:744-780 (api executions write)
* fill_contract_key_for_opt -- portfolio/reader/accounts_helpers.py:15-38 (read-time fallback)
* occ_local_symbol / contract_key_variants -- portfolio/reader/executions.py:586-642
"""
# ruff: noqa

from __future__ import annotations

import math
import re
from typing import Any, Dict, List


def positions_key(p: Dict[str, Any]) -> str:
    sym = p.get("symbol") or ""
    sec = p.get("secType") or p.get("sec_type") or ""
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
    return contract_key


def sink_execution_key(symbol, sec_type, source, expiry, strike, option_right, contract_key):
    sec_type_norm = (sec_type or "").strip().upper()
    if sec_type_norm == "OPT":
        sym_key = (symbol or "").strip()
        exp_val = expiry
        if isinstance(exp_val, (int, float)) and math.isfinite(exp_val):
            exp_key = str(int(exp_val))
        else:
            exp_key = (exp_val or "").strip().replace("-", "")
        strike_raw = strike
        try:
            strike_key = float(strike_raw) if strike_raw not in ("", None) else None
        except (TypeError, ValueError):
            strike_key = None
        right_key = (option_right or "").strip().upper()
        if len(right_key) > 1:
            right_key = "C" if right_key.startswith("C") else "P" if right_key.startswith("P") else right_key[:1]

        source_norm = (source or "").strip()
        if (
            source_norm in ("tws_event", "tws_client")
            and sym_key
            and exp_key
            and strike_key is not None
            and right_key
        ):
            exp_digits = "".join(ch for ch in exp_key if ch.isdigit())
            yymmdd = exp_digits[2:8] if len(exp_digits) >= 8 else exp_digits[-6:]
            try:
                strike_int = int(round(strike_key * 1000.0))
            except (TypeError, ValueError, OverflowError):
                strike_int = None
            if yymmdd and strike_int is not None:
                strike_8 = f"{strike_int:08d}"
                local_symbol = f"{sym_key}  {yymmdd}{right_key}{strike_8}"
                contract_key = "|".join(
                    [
                        local_symbol,
                        "OPT",
                        exp_key,
                        str(strike_key),
                        right_key,
                    ]
                )
        if not contract_key and sym_key:
            contract_key = "|".join(
                [
                    sym_key,
                    "OPT",
                    exp_key,
                    str(strike_key) if strike_key is not None else "",
                    right_key,
                ]
            )
    return contract_key


def accounts_execution_key(symbol, sec_type, source, expiry, strike, option_right, contract_key):
    sec_type_norm = (sec_type or "").strip().upper()
    if sec_type_norm == "OPT":
        source_norm = (source or "").strip()
        if source_norm in ("tws_event", "tws_client"):
            sym_key = (symbol or "").strip()
            exp_val = expiry
            if isinstance(exp_val, (int, float)) and math.isfinite(exp_val):
                exp_key = str(int(exp_val))
            else:
                exp_key = (exp_val or "").strip().replace("-", "")
            strike_raw = strike
            try:
                strike_key = float(strike_raw) if strike_raw not in ("", None) else None
            except (TypeError, ValueError):
                strike_key = None
            right_key = (option_right or "").strip().upper()
            if len(right_key) > 1:
                right_key = "C" if right_key.startswith("C") else "P" if right_key.startswith("P") else right_key[:1]
            if sym_key and exp_key and strike_key is not None and right_key:
                exp_digits = "".join(ch for ch in exp_key if ch.isdigit())
                yymmdd = exp_digits[2:8] if len(exp_digits) >= 8 else exp_digits[-6:]
                try:
                    strike_int = int(round(strike_key * 1000.0))
                except (TypeError, ValueError, OverflowError):
                    strike_int = None
                if yymmdd and strike_int is not None:
                    strike_8 = f"{strike_int:08d}"
                    local_symbol = f"{sym_key}  {yymmdd}{right_key}{strike_8}"
                    contract_key = "|".join(
                        [
                            local_symbol,
                            "OPT",
                            exp_key,
                            str(strike_key),
                            right_key,
                        ]
                    )
    return contract_key


def fill_contract_key_for_opt(d: Dict[str, Any]) -> None:
    """In-place: for OPT rows with missing contract_key, set contract_key from symbol|OPT|expiry|strike|option_right."""
    if (d.get("sec_type") or "").strip().upper() != "OPT":
        return
    ck = (d.get("contract_key") or "").strip()
    if ck:
        return
    sym = (d.get("symbol") or "").strip()
    exp = (d.get("expiry") or "")
    if isinstance(exp, (int, float)) and math.isfinite(exp):
        exp = str(int(exp))
    else:
        exp = (exp or "").strip().replace("-", "")
    strike = d.get("strike")
    if strike is not None and not isinstance(strike, str):
        strike = str(int(strike)) if strike is not None and math.isfinite(strike) else ""
    else:
        strike = (strike or "").strip()
    right = (d.get("option_right") or "").strip().upper()
    if len(right) > 1:
        right = "C" if right.startswith("C") else "P" if right.startswith("P") else right[:1]
    if not right and "right" in d:
        right = (d.get("right") or "").strip().upper()[:1] or ""
    d["contract_key"] = f"{sym}|OPT|{exp}|{strike}|{right}"


def occ_local_symbol(symbol: str, expiry_yyyymmdd: str, strike: float, right: str) -> str:
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


def contract_key_variants(contract_key: str) -> List[str]:
    """
    account_positions uses SYMBOL|OPT|YYYYMMDD|strike|C/P (e.g. RKLB|OPT|20260320|80.0|C).
    account_executions often uses OCC local|OPT|YYYYMMDD|strike|C/P
    (e.g. RKLB  260320C00080000|OPT|20260320|80.0|C).
    """
    ck = (contract_key or "").strip()
    parts = ck.split("|")
    if len(parts) < 5 or parts[1].strip().upper() != "OPT":
        return [ck]
    sym_seg, exp_raw, strike_raw, right_raw = parts[0], parts[2], parts[3], parts[4]
    # Execution-style OCC local (positions use short symbol, e.g. RKLB vs RKLB  260320C00080000)
    if len(sym_seg) > 6:
        return [ck]
    try:
        strike_f = float(strike_raw)
    except (TypeError, ValueError):
        return [ck]
    r = right_raw.strip().upper()[:1]
    if r not in ("C", "P"):
        r = "C"
    exp_digits = re.sub(r"\D", "", exp_raw)
    exp8 = exp_digits[:8] if len(exp_digits) >= 8 else exp_digits.ljust(8, "0")[:8]
    sym = re.split(r"\s+", sym_seg.strip())[0].upper()[:6]
    occ = occ_local_symbol(sym, exp8, strike_f, r)
    tails = list(
        dict.fromkeys(
            [
                strike_raw.strip(),
                str(int(strike_f)) if strike_f == int(strike_f) else strike_raw,
                f"{strike_f:.1f}",
                f"{strike_f:g}",
            ]
        )
    )
    keys: List[str] = [ck, f"{occ}|OPT|{exp8}|{strike_raw.strip()}|{r}"]
    for t in tails:
        keys.append(f"{occ}|OPT|{exp8}|{t}|{r}")
    return list(dict.fromkeys(keys))
