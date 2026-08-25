"""Wave 11: drop deprecated Flex token columns from settings."""

from __future__ import annotations

from typing import Any


def migrate_wave11_drop_flex_token_columns(cur: Any) -> None:
    """Remove plaintext Flex token columns (canonical source: K8s Secret only)."""
    cur.execute("ALTER TABLE settings DROP COLUMN IF EXISTS ib_flex_host_token")
    cur.execute("ALTER TABLE settings DROP COLUMN IF EXISTS ib_flex_secondary_token")
