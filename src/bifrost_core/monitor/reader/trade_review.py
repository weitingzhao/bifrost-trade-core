"""Trade reviews: one record per strategy instance.

Review › Queue and Review › Single trade read the same rows (design Rev .110):
an instance is *awaiting* until its review is stamped `reviewed_at`, and the
Review menu badge counts closed instances with no stamped review.

Whether an instance is closed is the fills' to say, not this table's -- the
caller decides when a review may be confirmed. This module only stores what
the trader decided, and it never deletes: reopening a review clears the stamp
and keeps the tags.

``patch_review`` (core 0.33.0, TD-15) is the PATCH writer; it raises ``Write*``.

No ``note`` since core 0.43.0 (TD-73): a trade's notes live in the Research journal,
and ``trade_review.note`` is dropped after this release. Neither writer reads or writes
it, and both refuse a ``note`` key rather than drop it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Optional

from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.errors import WriteFailed, WriteInvalid, WriteNotFound
from bifrost_core.monitor.reader.trade_names import add_review_tag_names, review_fields_as_columns

logger = logging.getLogger(__name__)

TAG_MAX = 40
TAG_LEN_MAX = 60

# Read back under the names the rows have carried since core 0.42.0 (``_row_out`` adds the
# new ones beside them); the columns are trade_id / tags_*_json since naming R3 (core 0.45.0).
_COLUMNS = """
    trade_review_id, trade_id AS strategy_instance_id,
    tags_added_json AS tags_added, tags_dropped_json AS tags_dropped,
    reviewed_at, created_at, updated_at
"""


def clean_tags(tags: Optional[Iterable[Any]]) -> Optional[List[str]]:
    """Trimmed, de-duplicated in order, bounded. None stays None (keep what is stored)."""
    if tags is None:
        return None
    out: List[str] = []
    for raw in tags:
        text = str(raw or "").strip()[:TAG_LEN_MAX]
        if text and text not in out:
            out.append(text)
        if len(out) >= TAG_MAX:
            break
    return out


def _conn_from_config(status_config: Optional[dict]) -> Any:
    """Open a connection from status_config (postgres). None when not configured or unreachable."""
    return ws.conn_from_config(status_config, "trade_review", log=logger)


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # pragma: no cover
        pass


def _row_out(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for key in ("tags_added", "tags_dropped"):
        raw = out.get(key)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = []
        out[key] = [str(t) for t in raw] if isinstance(raw, list) else []
    out["reviewed"] = out.get("reviewed_at") is not None
    # trade_id and tags_added_json / tags_dropped_json beside the column names (naming R1).
    return add_review_tag_names(out)


def list_reviews(status_config: Optional[dict]) -> List[Dict[str, Any]]:
    """Every review, newest change first. A failed read raises -- an empty list
    would say no instance has been reviewed, which is a statement about the book."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return []
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM trade_review ORDER BY updated_at DESC, trade_review_id DESC"
            )
            rows = cur.fetchall()
        return [_row_out(dict(r)) for r in rows]
    finally:
        _close(conn)


def save_review(
    status_config: Optional[dict],
    strategy_instance_id: int,
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Upsert one instance's review and return it.

    Fields left out of `payload` keep what is stored. `reviewed: true` stamps
    `reviewed_at` (a second confirm keeps the first stamp); `false` clears it.
    None when Postgres is not configured; raises when the instance does not
    exist (foreign key) or the write fails.
    """
    if "note" in payload:
        raise WriteInvalid(NOTE_RETIRED)
    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    added = clean_tags(payload.get("tags_added"))
    dropped = clean_tags(payload.get("tags_dropped"))
    reviewed = payload.get("reviewed")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                INSERT INTO trade_review (trade_id, tags_added_json, tags_dropped_json, reviewed_at)
                VALUES (
                    %(id)s,
                    COALESCE(%(added)s::jsonb, '[]'::jsonb),
                    COALESCE(%(dropped)s::jsonb, '[]'::jsonb),
                    CASE WHEN %(reviewed)s IS TRUE THEN now() ELSE NULL END
                )
                ON CONFLICT (trade_id) DO UPDATE SET
                    tags_added_json   = COALESCE(%(added)s::jsonb, trade_review.tags_added_json),
                    tags_dropped_json = COALESCE(%(dropped)s::jsonb, trade_review.tags_dropped_json),
                    reviewed_at  = CASE
                        WHEN %(reviewed)s IS TRUE THEN COALESCE(trade_review.reviewed_at, now())
                        WHEN %(reviewed)s IS FALSE THEN NULL
                        ELSE trade_review.reviewed_at
                    END,
                    updated_at   = now()
                RETURNING {_COLUMNS}
                """,
                {
                    "id": int(strategy_instance_id),
                    "added": None if added is None else json.dumps(added),
                    "dropped": None if dropped is None else json.dumps(dropped),
                    "reviewed": reviewed,
                },
            )
            row = cur.fetchone()
        conn.commit()
        return _row_out(dict(row)) if row else None
    except Exception:
        try:
            conn.rollback()
        except Exception:  # pragma: no cover
            pass
        raise
    finally:
        _close(conn)


# --- TD-15 writer (core 0.33.0): return the row / raise Write* ----------------------

REVIEW_PATCHABLE = ("tags_added", "tags_dropped", "reviewed")

NOTE_RETIRED = (
    "note was removed in core 0.43.0 (TD-73): a trade's notes live in the Research journal "
    "(POST /research/journal/notes with a ref of type 'inst')."
)


def _patch_tags(value: Any, name: str) -> List[str]:
    items = ws.list_value(value, name)
    out: List[str] = []
    for i, item in enumerate(items):
        tag = ws.text(item, f"{name}[{i}]", nullable=False, max_len=TAG_LEN_MAX) or ""
        if tag not in out:
            out.append(tag)
    if len(out) > TAG_MAX:
        raise WriteInvalid(f"{name} has {len(out)} tags; at most {TAG_MAX}.")
    return out


def patch_review(conn_or_config: Any, strategy_instance_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change one instance's review; return it in ``save_review``'s shape (with ``reviewed``).

    The review is keyed by the instance and has no state of its own before the first
    write, so a missing review row is created; a missing *instance* is WriteNotFound.
    ``tags_added`` / ``tags_dropped``: lists of text (trimmed, at most 60 characters each
    and 40 tags; a repeat is kept once), replaced whole, ``[]`` empties, null refused.
    ``reviewed``: true stamps ``reviewed_at`` (a
    second true keeps the first stamp), false clears it. Raises WriteInvalid,
    WriteNotFound, WriteFailed.
    """
    what = f"the review of trade {strategy_instance_id}"
    # tags_added_json / tags_dropped_json are taken too; the new name wins (naming R1).
    if isinstance(fields, dict) and "note" in fields:
        raise WriteInvalid(NOTE_RETIRED)
    fields = ws.check_fields(review_fields_as_columns(fields), REVIEW_PATCHABLE, "review")
    insert_cols: Dict[str, Any] = {}
    updates: List[str] = []
    values: Dict[str, Any] = {"id": int(strategy_instance_id)}
    for name in ("tags_added", "tags_dropped"):
        if name in fields:
            values[name] = json.dumps(_patch_tags(fields[name], name))
            insert_cols[f"{name}_json"] = f"%({name})s::jsonb"
            updates.append(f"{name}_json = EXCLUDED.{name}_json")
    if "reviewed" in fields:
        values["reviewed"] = ws.boolean(fields["reviewed"], "reviewed")
        insert_cols["reviewed_at"] = "CASE WHEN %(reviewed)s THEN now() ELSE NULL END"
        updates.append(
            "reviewed_at = CASE WHEN %(reviewed)s THEN COALESCE(trade_review.reviewed_at, now()) ELSE NULL END"
        )
    updates.append("updated_at = now()")
    columns = ["trade_id", *insert_cols.keys()]
    placeholders = ["%(id)s", *insert_cols.values()]
    sql = (
        f"INSERT INTO trade_review ({', '.join(columns)}) VALUES ({', '.join(placeholders)}) "
        f"ON CONFLICT (trade_id) DO UPDATE SET {', '.join(updates)} "
        f"RETURNING {_COLUMNS}"
    )
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what, on_fk="not_found"):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT 1 FROM trade WHERE trade_id = %(id)s",
                values,
            )
            if cur.fetchone() is None:
                raise WriteNotFound(f"No trade {strategy_instance_id}.")
            cur.execute(sql, values)
            row = cur.fetchone()
        if row is None:
            raise WriteFailed(f"{what} was written but not returned; nothing was saved.")
    return _row_out(dict(row))
