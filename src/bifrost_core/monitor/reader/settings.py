"""Settings: IB config. Conn-based and status_config-based APIs."""

import logging
from typing import Iterable, Any, Dict, Optional

from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader import write_support as ws

logger = logging.getLogger(__name__)

_ACTIVE_REF_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("active_gate_safety_strategy_id", "gate_safety_strategy", "gate_safety_strategy_id"),
    ("active_strategy_structure_id", "strategy_structure", "strategy_structure_id"),
    ("active_strategy_allocation_id", "strategy_allocation", "strategy_allocation_id"),
)


def validate_settings_active_refs(cur: Any, payload: Dict[str, Any]) -> None:
    """Raise ValueError if any non-null active_*_id in payload does not exist."""
    for field, table, pk_col in _ACTIVE_REF_CHECKS:
        ref_id = payload.get(field)
        if ref_id is None:
            continue
        cur.execute(
            f"SELECT 1 FROM {table} WHERE {pk_col} = %s",
            (int(ref_id),),
        )
        if cur.fetchone() is None:
            raise ValueError(f"{field}={ref_id} does not exist in {table}")


# ----- Conn-based (for common.StatusReader delegation) -----

def get_ib_config(conn: Any) -> Optional[Dict[str, Any]]:
    """Return settings row id=1: ib_host_account_id and stream account IDs.

    IB host/port/client IDs come from config YAML (see get_effective_ib_config), not from DB.
    The Flex range days are not read here since core 0.39.0: nothing used them (the HTTP
    boundary never output them) and the Flex Query plugin keeps them in Golden Source
    ``ops_jobs.flex_settings`` from 0.7.0 (TD-74). The ``settings.flex_*_range_days``
    columns stay until a later version drops them.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT ib_host_account_id, "
                "stream_host_account_id, stream_secondary_account_id FROM settings WHERE id = 1"
            )
            row = cur.fetchone()
        if row is None:
            return None
        out: Dict[str, Any] = {}
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
        conn = ws.open_conn(status_config)
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


_ACTIVE_COLUMNS = (
    "active_strategy_structure_id",
    "active_gate_safety_strategy_id",
    "active_strategy_allocation_id",
)


def write_active_strategy_and_gates(
    status_config: dict,
    active_strategy_structure_id: Optional[int] = None,
    active_gate_safety_strategy_id: Optional[int] = None,
    active_strategy_allocation_id: Optional[int] = None,
    *,
    only: Optional[Iterable[str]] = None,
) -> bool:
    """Update settings (id=1): the three ids the daemon loads on its next start.

    ``only`` names the columns to write; the others keep their value. Without it all
    three are written, None clearing a column. The API passes the body's set fields,
    so a caller sending only the allocation no longer clears the structure and gate
    the daemon would load (debt TD-38). Returns True on success.
    """
    values = {
        "active_strategy_structure_id": active_strategy_structure_id,
        "active_gate_safety_strategy_id": active_gate_safety_strategy_id,
        "active_strategy_allocation_id": active_strategy_allocation_id,
    }
    columns = list(_ACTIVE_COLUMNS) if only is None else [c for c in _ACTIVE_COLUMNS if c in set(only)]
    if not columns:
        return True
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return False
    try:
        conn = ws.open_conn(status_config)
        try:
            with conn.cursor() as cur:
                validate_settings_active_refs(cur, {c: values[c] for c in columns})
                assignments = ", ".join(f"{c} = %s" for c in columns)
                cur.execute(
                    f"UPDATE settings SET {assignments} WHERE id = 1",
                    tuple(values[c] for c in columns),
                )
            conn.commit()
            logger.info(
                "write_active_strategy_and_gates: %s",
                ", ".join(f"{c}={values[c]}" for c in columns),
            )
            return True
        finally:
            conn.close()
    except ValueError:
        raise
    except Exception as e:
        logger.warning("write_active_strategy_and_gates failed: %s", e)
        return False


__all__ = [
    "get_ib_config",
    "write_ib_config",
    "write_active_strategy_and_gates",
    "validate_settings_active_refs",
]
