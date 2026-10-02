"""Saved searches: a page's filters, kept under a name (preference_saved_search).

The Finder's smart folders, as trade design Rev .139 draws them: Plans' Save
as list keeps the page's scope (status, accounts, symbol, tokens) under a
label, and the sidebar lists every saved search on every page; a click opens
the page with that scope. Stored here rather than in the browser so they
follow the operator to another machine (Owner 2026-10-01).

Trade has no sign-in, so there is one operator: `owner` is 'operator' for
every row today, and is the column a future sign-in keys on (Owner
2026-10-01, single operator).

`state_json` is the page's own: this module neither reads nor checks it.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteInvalid, WriteNotFound
from bifrost_core.persistence.postgres.connection import _get_conn_params

logger = logging.getLogger(__name__)

OPERATOR = "operator"
_LABEL_MAX = 120
_ROUTE_MAX = 200


class SavedSearchError(WriteInvalid):
    """A save the table would refuse; ``reason`` is what to show.

    A ``WriteInvalid`` (and so still a ``ValueError``) since core 0.33.0.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


def _conn_from_config(status_config: Optional[dict]) -> Any:
    if not status_config or (status_config.get("sink") != "postgres" and not status_config.get("postgres")):
        return None
    try:
        return psycopg2.connect(**_get_conn_params(status_config))
    except Exception as e:  # pragma: no cover - connection failure path
        logger.warning("saved_search connect failed: %s", e)
        return None


def _table_exists(cur: Any) -> bool:
    cur.execute("SELECT to_regclass('public.preference_saved_search') IS NOT NULL AS ok")
    row = cur.fetchone()
    return bool(row["ok"] if isinstance(row, dict) else row[0])


def _row_out(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    raw = out.get("state_json")
    if isinstance(raw, str):
        try:
            out["state_json"] = json.loads(raw)
        except ValueError:
            out["state_json"] = {}
    elif raw is None:
        out["state_json"] = {}
    return out


def list_saved_searches(status_config: Optional[dict], owner: str = OPERATOR) -> List[Dict[str, Any]]:
    """Every saved search, oldest first. Empty where the table does not exist yet."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not _table_exists(cur):
                return []
            cur.execute(
                "SELECT preference_saved_search_id, route, label, state_json, created_at "
                "FROM preference_saved_search WHERE owner = %s "
                "ORDER BY created_at, preference_saved_search_id",
                (owner,),
            )
            return [_row_out(dict(r)) for r in cur.fetchall()]
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - close failure path
            pass


def create_saved_search(
    status_config: Optional[dict],
    route: str,
    label: str,
    state: Dict[str, Any],
    owner: str = OPERATOR,
) -> Optional[int]:
    """Keep one page scope under a label. A label already used on that page is
    replaced, so saving the same name twice does not make two rows."""
    route = str(route or "").strip()
    label = " ".join(str(label or "").split())
    if not route.startswith("/") or len(route) > _ROUTE_MAX:
        raise SavedSearchError("route must be an app path, like /trade/plans.")
    if not label or len(label) > _LABEL_MAX:
        raise SavedSearchError(f"label is required, at most {_LABEL_MAX} characters.")
    if not isinstance(state, dict):
        raise SavedSearchError("state must be an object.")
    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO preference_saved_search (owner, route, label, state_json) "
                "VALUES (%s, %s, %s, %s::jsonb) "
                "ON CONFLICT (owner, route, label) DO UPDATE "
                "SET state_json = EXCLUDED.state_json, updated_at = now() "
                "RETURNING preference_saved_search_id",
                (owner, route, label, json.dumps(state)),
            )
            new_id = int(cur.fetchone()[0])
        conn.commit()
        return new_id
    except Exception:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - rollback failure path
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - close failure path
            pass


def delete_saved_search(status_config: Optional[dict], saved_search_id: int, owner: str = OPERATOR) -> bool:
    """Forget one saved search. False when there is no such row."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM preference_saved_search WHERE preference_saved_search_id = %s AND owner = %s",
                (saved_search_id, owner),
            )
            gone = cur.rowcount > 0
        conn.commit()
        return gone
    except Exception:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover - rollback failure path
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - close failure path
            pass


def delete_saved_search_strict(conn_or_config: Any, saved_search_id: int, owner: str = OPERATOR) -> Dict[str, Any]:
    """``delete_saved_search`` with outcomes (core 0.33.0, TD-15):
    ``{"deleted": "hard", "preference_saved_search_id"}``, or WriteNotFound (no such row
    for this owner) / WriteFailed (not configured, unreachable, statement failed)."""
    what = f"saved search {saved_search_id}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM preference_saved_search WHERE preference_saved_search_id = %s AND owner = %s",
                (saved_search_id, owner),
            )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No saved search {saved_search_id}.")
    return {"deleted": "hard", "preference_saved_search_id": saved_search_id}
