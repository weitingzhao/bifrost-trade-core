"""Shared Redis health-hash writer for all socket edge services.

Every socket service writes a ``bifrost:health:ws_*`` hash to Redis so the
Monitor dashboard can show connection status.  This module provides a common
base that:

- Appends ``updated_at``, ``env``, and ``config_file`` on every write (fixes B4:
  dashboard can now distinguish Dev from Prod).
- Refreshes the hash TTL on every write (default 180 s).
- Serialises all values to strings (Redis hash fields must be strings).
"""

from __future__ import annotations

import time
from typing import Any, Dict


_DEFAULT_TTL = 180  # seconds — refresh every ~30 s heartbeat keeps it alive


class HealthHashWriter:
    """Write a Redis Hash health record for a single socket service.

    Subclass this to add service-specific helper methods::

        class IbIngestorHealthWriter(HealthHashWriter):
            def write_connected(self, *, client_id: int, msg_count: int) -> None:
                self.write({
                    "connected": "1",
                    "client_id": str(client_id),
                    "msg_count": str(msg_count),
                })
    """

    def __init__(
        self,
        redis_client: Any,
        key: str,
        *,
        ttl: int = _DEFAULT_TTL,
        env: str = "",
        config_file: str = "",
    ) -> None:
        self._r = redis_client
        self._key = key
        self._ttl = ttl
        self._env = env
        self._config_file = config_file

    # ── public API ────────────────────────────────────────────────────────────

    def write(self, fields: Dict[str, Any]) -> None:
        """Write *fields* to the hash and refresh its TTL.

        Automatically adds ``updated_at``, ``env``, and ``config_file``.
        All values are coerced to strings before writing.
        """
        mapping = {k: _to_str(v) for k, v in fields.items()}
        mapping["updated_at"] = _to_str(time.time())
        if self._env:
            mapping["env"] = self._env
        if self._config_file:
            mapping["config_file"] = self._config_file
        self._r.hset(self._key, mapping=mapping)
        self._r.expire(self._key, self._ttl)

    def delete(self) -> None:
        """Remove the hash entirely (e.g. on clean shutdown)."""
        self._r.delete(self._key)

    # ── convenience ───────────────────────────────────────────────────────────

    @property
    def key(self) -> str:
        return self._key

    @property
    def env(self) -> str:
        return self._env


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_str(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if v is None:
        return ""
    return str(v)
