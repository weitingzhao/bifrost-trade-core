"""Position book: wraps Store + get_option_legs for state space."""

from typing import Any, List

from bifrost_core.portfolio.positions.portfolio import OptionLeg, get_option_legs


class PositionBook:
    """Exposes option_legs and stock_shares from current store."""

    def __init__(
        self,
        store: Any,
        symbol: str,
        min_dte: int = 21,
        max_dte: int = 35,
        atm_band_pct: float = 0.03,
    ):
        self._store = store
        self._symbol = symbol
        self._min_dte = min_dte
        self._max_dte = max_dte
        self._atm_band_pct = atm_band_pct

    def set_symbol(self, symbol: str) -> None:
        self._symbol = symbol

    @property
    def option_legs(self) -> List[OptionLeg]:
        positions = self._store.get_positions()
        spot = self._store.get_underlying_price()
        legs = get_option_legs(
            positions,
            self._symbol,
            min_dte=self._min_dte,
            max_dte=self._max_dte,
            atm_band_pct=self._atm_band_pct,
            spot=spot,
        )
        return legs

    @property
    def stock_shares(self) -> int:
        return self._store.get_stock_position()
