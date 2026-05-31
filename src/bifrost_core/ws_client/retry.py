"""Shared WebSocket reconnect backoff policy for all socket edge services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class ReconnectPolicy:
    """Exponential backoff with a configurable cap.

    Usage::

        policy = ReconnectPolicy.from_config(cfg)
        attempt = 0
        while running:
            try:
                await connect_and_run()
                attempt = 0          # reset after a clean session
            except Exception:
                attempt += 1
                await asyncio.sleep(policy.delay_for_attempt(attempt))
    """

    initial_delay: float = 2.0
    max_delay: float = 60.0
    backoff_factor: float = 2.0
    max_exp: int = 6

    def delay_for_attempt(self, attempt: int) -> float:
        """Return backoff seconds for the given 1-based attempt number."""
        if attempt < 1:
            attempt = 1
        exp = min(attempt - 1, self.max_exp)
        return min(self.initial_delay * (self.backoff_factor ** exp), self.max_delay)

    @classmethod
    def from_config(cls, config: Dict[str, Any]) -> "ReconnectPolicy":
        """Construct from merged YAML config (reads ``ib_connection`` block)."""
        raw = config.get("ib_connection") or {}

        def _f(key: str, default: float) -> float:
            v = raw.get(key)
            try:
                return max(0.0, float(v)) if v is not None else default
            except (TypeError, ValueError):
                return default

        def _i(key: str, default: int) -> int:
            v = raw.get(key)
            try:
                return max(0, int(v)) if v is not None else default
            except (TypeError, ValueError):
                return default

        initial = _f("reconnect_base_sec", 2.0)
        max_d = _f("reconnect_max_sec", 60.0)
        max_exp = _i("reconnect_max_exp", 6)
        # backoff_factor not in legacy YAML — keep default 2.0
        return cls(
            initial_delay=max(0.5, initial),
            max_delay=max(initial, max_d),
            backoff_factor=2.0,
            max_exp=max_exp,
        )
