"""Settings: IB config. Conn-based and status_config-based APIs."""

import logging
from typing import Any, Dict, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)


# ----- Conn-based (for common.StatusReader delegation) -----

def get_ib_config(conn: Any) -> Optional[Dict[str, Any]]:
    """Return settings row id=1: ib_host_account_id, flex ranges, stream account IDs.

    IB host/port/client IDs come from config YAML (see get_effective_ib_config), not from DB.
    Flex range days remain here because they share the settings row; Flex token/query
    R/W lives in the Flex Query Plugin.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT ib_host_account_id, flex_default_range_days, flex_init_range_days, "
                "stream_host_account_id, stream_secondary_account_id FROM settings WHERE id = 1"
            )
            row = cur.fetchone()
        if row is None:
            return None
        out: Dict[str, Any] = {}
        if row.get("flex_default_range_days") is not None:
            try:
                out["flex_default_range_days"] = max(1, int(row["flex_default_range_days"]))
            except (TypeError, ValueError):
                out["flex_default_range_days"] = 30
        else:
            out["flex_default_range_days"] = 30
        if row.get("flex_init_range_days") is not None:
            try:
                out["flex_init_range_days"] = max(1, int(row["flex_init_range_days"]))
            except (TypeError, ValueError):
                out["flex_init_range_days"] = 360
        else:
            out["flex_init_range_days"] = 360
        if row.get("ib_host_account_id") is not None and str(row.get("ib_host_account_id")).strip():
            out["ib_host_account_id"] = str(row["ib_host_account_id"]).strip()
        else:
            out["ib_host_account_id"] = None
        if row.get("stream_host_account_id") is not None and str(row.get("stream_host_account_id")).strip():
            out["stream_host_account_id"] = str(row["stream_host_account_id"]).strip()
        else:
            out["stream_host_account_id"] = None
        if row.get("stream_secondary_account_id") is not None and str(row.get("stream_secondary_account_id")).strip():
            out["stream_secondary_account_id"] = str(row["stream_secondary_account_id"]).strip()
        else:
            out["stream_secondary_account_id"] = None
        return out
    except Exception as e:
        logger.debug("get_ib_config failed: %s", e)
        return None


# ----- Module-level (status_config) for re-export -----

def write_ib_config(
    status_config: dict,
    ib_host_account_id: Optional[str] = None,
    stream_host_account_id: Optional[str] = None,
    stream_secondary_account_id: Optional[str] = None,
) -> bool:
    """Update settings (id=1): ib_host_account_id, stream_*_account_id. IB host/port/client IDs are not stored in DB."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    host_val = (ib_host_account_id or "").strip() or None
    stream_host_val = (stream_host_account_id or "").strip() or None
    stream_secondary_val = (stream_secondary_account_id or "").strip() or None
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE settings SET
                        ib_host_account_id = %s,
                        stream_host_account_id = %s,
                        stream_secondary_account_id = %s
                    WHERE id = 1
                    """,
                    (host_val, stream_host_val, stream_secondary_val),
                )
                if cur.rowcount == 0:
                    cur.execute(
                        """
                        INSERT INTO settings (id, ib_host_account_id, stream_host_account_id, stream_secondary_account_id)
                        VALUES (1, %s, %s, %s)
                        """,
                        (host_val, stream_host_val, stream_secondary_val),
                    )
            conn.commit()
            logger.info("[R-A3] write_ib_config: wrote settings id=1 account/stream fields")
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_ib_config failed: %s", e)
        return False


def write_active_strategy_and_gates(
    status_config: dict,
    active_strategy_structure_id: Optional[int] = None,
    active_gate_safety_strategy_id: Optional[int] = None,
    active_strategy_allocation_id: Optional[int] = None,
) -> bool:
    """Update settings (id=1): active_strategy_structure_id, active_gate_safety_strategy_id, active_strategy_allocation_id. Returns True on success."""
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        params = _get_conn_params(status_config)
        conn = psycopg2.connect(**params)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE settings SET
                        active_strategy_structure_id = %s,
                        active_gate_safety_strategy_id = %s,
                        active_strategy_allocation_id = %s
                    WHERE id = 1
                    """,
                    (active_strategy_structure_id, active_gate_safety_strategy_id, active_strategy_allocation_id),
                )
            conn.commit()
            logger.info(
                "write_active_strategy_and_gates: active_strategy_structure_id=%s active_gate_safety_strategy_id=%s active_strategy_allocation_id=%s",
                active_strategy_structure_id, active_gate_safety_strategy_id, active_strategy_allocation_id,
            )
            return True
        finally:
            conn.close()
    except Exception as e:
        logger.warning("write_active_strategy_and_gates failed: %s", e)
        return False


__all__ = [
    "write_ib_config",
    "write_active_strategy_and_gates",
]
