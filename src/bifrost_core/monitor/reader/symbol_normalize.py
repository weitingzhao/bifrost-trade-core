"""Normalize stock/index symbols for config ↔ PostgreSQL ``stock_* .symbol`` matching."""

from __future__ import annotations

import unicodedata

# U+FF3E FULLWIDTH CIRCUMFLEX ACCENT (looks like ^ in some fonts / copy-paste)
_FULLWIDTH_CIRCUMFLEX = "\uff3e"


def norm_bars_symbol(s: str) -> str:
    """Trim, NFKC, map fullwidth ``＾`` → ASCII ``^``, then uppercase (match UI + SQL)."""
    t = unicodedata.normalize("NFKC", (s or "").strip())
    if _FULLWIDTH_CIRCUMFLEX in t:
        t = t.replace(_FULLWIDTH_CIRCUMFLEX, "^")
    return t.upper()
