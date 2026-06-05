"""Position/symbol parsing and active symbol inference. Used by GsTrading."""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def position_symbol_parts(item: Any) -> tuple[str, str]:
    """Extract (symbol, sec_type) from one position item."""
    contract = None
    if hasattr(item, "contract"):
        contract = item.contract
    elif isinstance(item, dict):
        contract = item.get("contract")
    if contract is None:
        return "", ""
    if isinstance(contract, dict):
        symbol = str(contract.get("symbol") or "").strip().upper()
        sec_type = str(
            contract.get("secType") or contract.get("sec_type") or ""
        ).strip().upper()
    else:
        symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
        sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
    return symbol, sec_type


def infer_active_symbol(app: Any, positions: list[Any]) -> str:
    """Prefer option underlying symbol, then stock symbol, from current positions."""
    first_stock = ""
    for item in positions:
        symbol, sec_type = position_symbol_parts(item)
        if not symbol:
            continue
        if sec_type == "OPT":
            return symbol
        if not first_stock and sec_type == "STK":
            first_stock = symbol
    return first_stock


def set_active_symbol(app: Any, symbol: Optional[str]) -> None:
    """Switch the strategy symbol when live positions change."""
    next_symbol = (symbol or "").strip().upper()
    if next_symbol == app.symbol:
        return
    prev_symbol = app.symbol
    app.symbol = next_symbol
    app._position_book.set_symbol(next_symbol)
    # Clear quote cache so we never reuse the previous symbol's market data.
    app.store.set_underlying_quote(None, None)
    app.store.set_underlying_price(None)
    if next_symbol:
        logger.info(
            "Active symbol updated from positions: %s -> %s",
            prev_symbol or "(none)",
            next_symbol,
        )
    elif prev_symbol:
        logger.info("Active symbol cleared (previous=%s)", prev_symbol)
