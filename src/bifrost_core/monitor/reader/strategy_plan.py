"""Structured trade plans: read, write, and the one place their rules live.

A plan records what the desk intends -- legs, size, and how it means to get out
-- so that afterwards there is something to compare the fill against. It is
advisory: nothing reads this table to act. The daemon and the gateway do not
know it exists (D10).

The rules are enforced here rather than in the API so there is one answer to
"may this change happen", whichever caller asks:

    create  ->  draft
    draft   ->  intended    (needs a leg, and a written exit)
    draft   ->  cancelled
    intended -> filled      (linked to an instance of the same account)
    intended -> cancelled

Once intended, the content is frozen: the plan is the thing being judged, so
editing it after the fact would remove the judgement. Roll it instead -- a new
plan with `source_kind='roll'` and `parent_strategy_plan_id` set. There is no
delete.

One exception, in ``patch_plan`` only (core 0.33.0): an intended plan may change its
``expires_at`` and nothing else -- that is what the plan card's "Extend 7 days" and
"Re-issue intent" buttons do. ``update_plan`` (PUT) still refuses it.

`expired` is not a stored status. An intent whose `expires_at` has passed reads
as expired, and can still be linked to a fill or cancelled; the row keeps
saying `intended` because that is what happened.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

from bifrost_core.monitor.reader import write_support as ws
from bifrost_core.monitor.reader.trade_names import add_trade_names
from bifrost_core.monitor.reader.errors import WriteConflict, WriteFailed, WriteInvalid, WriteNotFound

logger = logging.getLogger(__name__)

PLAN_STATUSES = ("draft", "intended", "filled", "cancelled")
PLAN_EFFECTIVE_STATUSES = ("draft", "intended", "expired", "filled", "cancelled")

_LEG_SIDES = ("buy", "sell")
_LEG_SEC_TYPES = ("OPT", "STK")
_LEG_RIGHTS = ("C", "P")

# `filled_at` is read, not stored (TD-43, core 0.41.0): the linked instance's `opened_at`,
# so moving the instance's open moves it too. Only a filled plan has an instance (CHECK
# strategy_plan_filled_instance_ck), so every other plan reads null. The column is not named
# anywhere since core 0.43.0 and is dropped by an Owner db-step after that release.
# Plan `p` LEFT JOIN trade `i`; filters and order name `p.`.
_PLAN_COLUMNS = """
    p.strategy_plan_id, p.account_id, p.symbol, p.structure_label,
    p.strategy_structure_id, p.strategy_opportunity_id,
    p.legs_json, p.qty, p.price_effect, p.limit_price,
    p.target_kind, p.target_value, p.stop_kind, p.stop_value, p.exit_by,
    p.rationale, p.source_kind, p.source_ref, p.source_json,
    p.status, p.expires_at, p.intended_at, i.opened_at AS filled_at, p.cancelled_at,
    p.trade_id AS strategy_instance_id, p.parent_strategy_plan_id, p.created_at, p.updated_at
"""
_PLAN_FROM = "strategy_plan p LEFT JOIN trade i ON i.trade_id = p.trade_id"

# Columns a draft may replace. `status` and the timestamps are the state
# machine's, not the caller's.
_EDITABLE_COLUMNS = (
    "account_id",
    "symbol",
    "structure_label",
    "strategy_structure_id",
    "strategy_opportunity_id",
    "qty",
    "price_effect",
    "limit_price",
    "target_kind",
    "target_value",
    "stop_kind",
    "stop_value",
    "exit_by",
    "rationale",
    "source_kind",
    "source_ref",
    "expires_at",
)


class PlanRuleError(WriteConflict, ValueError):
    """A plan rule said no, and `reason` is what to show the reader.

    A ``WriteConflict`` (409) since core 0.33.0 -- the old writers raise it for
    state refusals and for bad input alike; ``patch_plan`` / ``delete_plan_strict``
    raise ``WriteInvalid`` for input and ``WriteConflict`` for state instead.
    Still a ``ValueError`` for the callers that catch it that way.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


def plan_effective_status(
    status: Optional[str], expires_at: Any = None, now: Optional[datetime] = None
) -> str:
    """The status a reader should see. `intended` past its expiry reads `expired`."""
    current = (status or "").strip()
    if current != "intended" or expires_at is None:
        return current
    if not hasattr(expires_at, "timestamp"):
        return current
    moment = now or datetime.now(timezone.utc)
    deadline = expires_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return "expired" if deadline < moment else current


def plan_exit_is_written(
    target_kind: Optional[str], stop_kind: Optional[str], exit_by: Any
) -> bool:
    """Whether the plan says anything at all about getting out."""
    return bool(target_kind) or bool(stop_kind) or exit_by is not None


def normalize_plan_legs(value: Any) -> List[Dict[str, Any]]:
    """Validate and normalise the legs array. Raises `PlanRuleError` on junk."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlanRuleError("legs must be a list")
    legs: List[Dict[str, Any]] = []
    for i, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise PlanRuleError(f"leg {i + 1} is not an object")
        side = str(raw.get("side") or "").strip().lower()
        if side not in _LEG_SIDES:
            raise PlanRuleError(f"leg {i + 1}: side must be buy or sell")
        sec_type = str(raw.get("sec_type") or "").strip().upper()
        if sec_type not in _LEG_SEC_TYPES:
            raise PlanRuleError(f"leg {i + 1}: sec_type must be OPT or STK")
        right = raw.get("right")
        right = str(right).strip().upper() if right not in (None, "") else None
        if right is not None and right not in _LEG_RIGHTS:
            raise PlanRuleError(f"leg {i + 1}: right must be C or P")
        strike = raw.get("strike")
        expiry = raw.get("expiry")
        expiry = str(expiry).strip() if expiry not in (None, "") else None
        if sec_type == "OPT":
            if right is None or strike is None or expiry is None:
                raise PlanRuleError(f"leg {i + 1}: an option leg needs right, strike and expiry")
        if expiry is not None:
            try:
                datetime.strptime(expiry, "%Y-%m-%d")
            except ValueError:
                raise PlanRuleError(f"leg {i + 1}: expiry must be YYYY-MM-DD") from None
        try:
            ratio = int(raw.get("ratio") if raw.get("ratio") is not None else 1)
        except (TypeError, ValueError):
            raise PlanRuleError(f"leg {i + 1}: ratio must be a whole number") from None
        if ratio < 1:
            raise PlanRuleError(f"leg {i + 1}: ratio must be 1 or more")
        legs.append(
            {
                "side": side,
                "sec_type": sec_type,
                "right": right,
                "strike": float(strike) if strike is not None else None,
                "expiry": expiry,
                "ratio": ratio,
                "contract_key": (str(raw.get("contract_key")).strip() or None)
                if raw.get("contract_key")
                else None,
                "mid_at_plan": float(raw["mid_at_plan"]) if raw.get("mid_at_plan") is not None else None,
                "quote_asof": str(raw["quote_asof"]) if raw.get("quote_asof") else None,
            }
        )
    return legs


def _normalize_source(value: Any) -> List[Dict[str, Any]]:
    """The provenance chain, as written when the plan was made."""
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _conn_from_config(status_config: Optional[dict]) -> Any:
    """Open a connection from status_config (postgres). None when not configured or unreachable."""
    return ws.conn_from_config(status_config, "strategy_plan", log=logger)


def _row_out(row: Dict[str, Any]) -> Dict[str, Any]:
    """One plan as callers read it, with the status they should show."""
    out = dict(row)
    for key in ("legs_json", "source_json"):
        raw = out.get(key)
        if isinstance(raw, str):
            try:
                out[key] = json.loads(raw)
            except ValueError:
                out[key] = []
        elif raw is None:
            out[key] = []
    out["effective_status"] = plan_effective_status(out.get("status"), out.get("expires_at"))
    return add_trade_names(out)  # trade_id beside strategy_instance_id (naming R1)


def list_plans(
    status_config: Optional[dict],
    status: Optional[str] = None,
    symbol: Optional[str] = None,
    account_id: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Plans, newest first. `status` filters the stored status, not the effective one."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return []
    conditions: List[str] = []
    values: List[Any] = []
    if status and str(status).strip():
        conditions.append("p.status = %s")
        values.append(str(status).strip())
    if symbol and str(symbol).strip():
        conditions.append("upper(p.symbol) = upper(%s)")
        values.append(str(symbol).strip())
    if account_id and str(account_id).strip():
        conditions.append("p.account_id = %s")
        values.append(str(account_id).strip())
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    values.append(max(1, int(limit)))
    # A failed read raises. Returning [] would tell the desk it has no plans,
    # which is a statement about the account, not about the query -- and it hid a
    # missing table behind a green `{"items": [], "count": 0}` once already.
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"SELECT {_PLAN_COLUMNS} FROM {_PLAN_FROM}{where} "
                "ORDER BY p.created_at DESC, p.strategy_plan_id DESC LIMIT %s",
                values,
            )
            rows = cur.fetchall()
        return [_row_out(dict(r)) for r in rows]
    finally:
        _close(conn)


def get_plan(status_config: Optional[dict], strategy_plan_id: int) -> Optional[Dict[str, Any]]:
    """One plan, or None when there is no such row. A failed read raises."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    try:
        return _get_plan_on(conn, strategy_plan_id)
    finally:
        _close(conn)


def _get_plan_on(conn: Any, strategy_plan_id: int) -> Optional[Dict[str, Any]]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_PLAN_COLUMNS} FROM {_PLAN_FROM} WHERE p.strategy_plan_id = %s",
            (strategy_plan_id,),
        )
        row = cur.fetchone()
    return _row_out(dict(row)) if row else None


def create_plan(status_config: Optional[dict], payload: Dict[str, Any]) -> Optional[int]:
    """Insert one draft. Returns its id, or None when Postgres is not configured."""
    fields = _plan_fields(payload, require=True)
    legs = normalize_plan_legs(payload.get("legs"))
    source = _normalize_source(payload.get("source"))
    conn = _conn_from_config(status_config)
    if conn is None:
        return None
    columns = [*fields.keys(), "legs_json", "source_json"]
    placeholders = ", ".join(["%s"] * len(fields) + ["%s::jsonb", "%s::jsonb"])
    values = [*fields.values(), json.dumps(legs), json.dumps(source)]
    parent = payload.get("parent_strategy_plan_id")
    if parent is not None:
        columns.append("parent_strategy_plan_id")
        placeholders += ", %s"
        values.append(int(parent))
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO strategy_plan ({', '.join(columns)}) VALUES ({placeholders}) "
                "RETURNING strategy_plan_id",
                values,
            )
            row = cur.fetchone()
        conn.commit()
        return int(row[0]) if row and row[0] is not None else None
    except Exception as e:
        logger.warning("create_plan failed: %s", e)
        _rollback(conn)
        raise
    finally:
        _close(conn)


def update_plan(
    status_config: Optional[dict], strategy_plan_id: int, payload: Dict[str, Any]
) -> bool:
    """Replace a draft's editable fields. Raises `PlanRuleError` past draft."""
    fields = _plan_fields(payload, require=False)
    legs = normalize_plan_legs(payload["legs"]) if "legs" in payload else None
    source = _normalize_source(payload["source"]) if "source" in payload else None
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        current = _locked_status(conn, strategy_plan_id)
        if current is None:
            return False
        if current != "draft":
            raise PlanRuleError(
                f"This plan is {current}, and only a draft can be edited. "
                "Cancel it and write a new one, or roll it."
            )
        sets = [f"{name} = %s" for name in fields]
        values: List[Any] = list(fields.values())
        if legs is not None:
            sets.append("legs_json = %s::jsonb")
            values.append(json.dumps(legs))
        if source is not None:
            sets.append("source_json = %s::jsonb")
            values.append(json.dumps(source))
        if not sets:
            conn.rollback()
            return True
        sets.append("updated_at = now()")
        values.append(strategy_plan_id)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE strategy_plan SET {', '.join(sets)} WHERE strategy_plan_id = %s",
                values,
            )
            updated = cur.rowcount > 0
        conn.commit()
        return updated
    except PlanRuleError:
        _rollback(conn)
        raise
    except Exception as e:
        logger.warning("update_plan failed: %s", e)
        _rollback(conn)
        raise
    finally:
        _close(conn)


def intend_plan(status_config: Optional[dict], strategy_plan_id: int) -> bool:
    """Mark a draft intended. Raises `PlanRuleError` with what is missing."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT status, legs_json, target_kind, stop_kind, exit_by "
                "FROM strategy_plan WHERE strategy_plan_id = %s FOR UPDATE",
                (strategy_plan_id,),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False
            plan = _row_out(dict(row))
            if plan["status"] != "draft":
                raise PlanRuleError(
                    f"This plan is {plan['status']}, and only a draft can be marked intended."
                )
            if not plan["legs_json"]:
                raise PlanRuleError("Write at least one leg before marking this intended.")
            if not plan_exit_is_written(plan["target_kind"], plan["stop_kind"], plan["exit_by"]):
                raise PlanRuleError(
                    "Write a target, a stop or an exit-by date. Without one there is "
                    "nothing to compare the outcome against."
                )
            cur.execute(
                "UPDATE strategy_plan SET status = 'intended', intended_at = now(), "
                "updated_at = now() WHERE strategy_plan_id = %s",
                (strategy_plan_id,),
            )
        conn.commit()
        return True
    except PlanRuleError:
        _rollback(conn)
        raise
    except Exception as e:
        logger.warning("intend_plan failed: %s", e)
        _rollback(conn)
        raise
    finally:
        _close(conn)


def link_fill(
    status_config: Optional[dict], strategy_plan_id: int, strategy_instance_id: int
) -> bool:
    """Say which instance an intent turned into. `filled_at` reads as the instance's own open
    (not stored since core 0.41.0); the table's CHECK holds `filled` and the instance together."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT status, account_id FROM strategy_plan "
                "WHERE strategy_plan_id = %s FOR UPDATE",
                (strategy_plan_id,),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False
            if row["status"] != "intended":
                raise PlanRuleError(
                    f"This plan is {row['status']}. Only an intended plan can be linked to a fill."
                )
            cur.execute(
                "SELECT account_id FROM trade "
                "WHERE trade_id = %s",
                (strategy_instance_id,),
            )
            instance = cur.fetchone()
            if instance is None:
                raise PlanRuleError(f"No trade {strategy_instance_id}.")
            if str(instance["account_id"]) != str(row["account_id"]):
                raise PlanRuleError(
                    f"That instance belongs to account {instance['account_id']}, "
                    f"and the plan to {row['account_id']}."
                )
            cur.execute(
                "UPDATE strategy_plan SET status = 'filled', trade_id = %s, "
                "updated_at = now() WHERE strategy_plan_id = %s",
                (strategy_instance_id, strategy_plan_id),
            )
        conn.commit()
        return True
    except PlanRuleError:
        _rollback(conn)
        raise
    except Exception as e:
        logger.warning("link_fill failed: %s", e)
        _rollback(conn)
        raise
    finally:
        _close(conn)


def cancel_plan(status_config: Optional[dict], strategy_plan_id: int) -> bool:
    """Drop a plan that will not be taken. Filled plans stay as they are."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        current = _locked_status(conn, strategy_plan_id)
        if current is None:
            return False
        if current not in ("draft", "intended"):
            raise PlanRuleError(f"This plan is {current}, and cannot be cancelled.")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE strategy_plan SET status = 'cancelled', cancelled_at = now(), "
                "updated_at = now() WHERE strategy_plan_id = %s",
                (strategy_plan_id,),
            )
        conn.commit()
        return True
    except PlanRuleError:
        _rollback(conn)
        raise
    except Exception as e:
        logger.warning("cancel_plan failed: %s", e)
        _rollback(conn)
        raise
    finally:
        _close(conn)


def delete_plan(status_config: Optional[dict], strategy_plan_id: int) -> bool:
    """Remove a draft. Only a draft goes: an intent, a fill or a cancellation is
    a record, so those are refused with the state that says no. The UI deletes
    without asking and offers Undo by holding the call until its toast closes
    (design Rev .138), so this is the last step, not a soft one."""
    conn = _conn_from_config(status_config)
    if conn is None:
        return False
    try:
        current = _locked_status(conn, strategy_plan_id)
        if current is None:
            return False
        if current != "draft":
            raise PlanRuleError(f"This plan is {current}; only a draft can be deleted.")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM strategy_plan WHERE strategy_plan_id = %s", (strategy_plan_id,))
        conn.commit()
        return True
    except PlanRuleError:
        _rollback(conn)
        raise
    except Exception as e:
        logger.warning("delete_plan failed: %s", e)
        _rollback(conn)
        raise
    finally:
        _close(conn)


_REQUIRED_ON_CREATE = ("account_id", "symbol", "structure_label", "qty")


def _plan_fields(payload: Dict[str, Any], require: bool) -> Dict[str, Any]:
    """The editable columns `payload` names, validated. On create, the four musts."""
    fields: Dict[str, Any] = {}
    for name in _EDITABLE_COLUMNS:
        must = require and name in _REQUIRED_ON_CREATE
        if name not in payload and not must:
            continue
        value = payload.get(name)
        if name in ("account_id", "symbol", "structure_label"):
            text = str(value or "").strip()
            if not text:
                raise PlanRuleError(f"{name.replace('_', ' ')} is required")
            fields[name] = text.upper() if name == "symbol" else text
        elif name == "qty":
            if value is None:
                raise PlanRuleError("qty is required")
            try:
                qty = int(value)
            except (TypeError, ValueError):
                raise PlanRuleError("qty must be a whole number") from None
            if qty <= 0:
                raise PlanRuleError("qty must be 1 or more")
            fields[name] = qty
        else:
            fields[name] = value
    if require:
        fields.setdefault("source_kind", payload.get("source_kind") or "manual")
    _check_pairs(fields)
    return fields


def _check_pairs(fields: Dict[str, Any]) -> None:
    """A target or stop is a kind *and* a value; the table says so too."""
    for kind, value, label in (
        ("target_kind", "target_value", "target"),
        ("stop_kind", "stop_value", "stop"),
    ):
        if kind in fields or value in fields:
            if (fields.get(kind) is None) != (fields.get(value) is None):
                raise PlanRuleError(f"A {label} needs both a kind and a value")


def _locked_status(conn: Any, strategy_plan_id: int) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM strategy_plan WHERE strategy_plan_id = %s FOR UPDATE",
            (strategy_plan_id,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _rollback(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:  # pragma: no cover - rollback failure path
        pass


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # pragma: no cover - close failure path
        pass


# --- TD-15 writers (core 0.33.0): return the row / raise Write* ---------------------

PLAN_PATCHABLE = (*_EDITABLE_COLUMNS, "legs", "source")
PLAN_PATCHABLE_WHEN_INTENDED = ("expires_at",)
_PRICE_EFFECTS = ("credit", "debit")
_TARGET_KINDS = ("credit_pct", "option_price", "underlying_price")
_STOP_KINDS = ("credit_multiple", "option_price", "underlying_price")
_SOURCE_KINDS = ("manual", "symbol", "hypothesis", "inbox_draft", "roll")


def _patch_plan_columns(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Validate each sent field into its column value. Every refusal is WriteInvalid."""
    cols: Dict[str, Any] = {}
    for name in ("account_id", "structure_label"):
        if name in fields:
            cols[name] = ws.text(fields[name], name, nullable=False)
    if "symbol" in fields:
        cols["symbol"] = (ws.text(fields["symbol"], "symbol", nullable=False) or "").upper()
    for name in ("strategy_structure_id", "strategy_opportunity_id"):
        if name in fields:
            cols[name] = ws.row_id(fields[name], name, nullable=True)
    if "qty" in fields:
        cols["qty"] = ws.integer(fields["qty"], "qty", nullable=False, minimum=1)
    if "price_effect" in fields:
        cols["price_effect"] = ws.choice(fields["price_effect"], "price_effect", _PRICE_EFFECTS, nullable=True)
    if "limit_price" in fields:
        cols["limit_price"] = ws.number(fields["limit_price"], "limit_price", nullable=True, minimum=0)
    if "target_kind" in fields:
        cols["target_kind"] = ws.choice(fields["target_kind"], "target_kind", _TARGET_KINDS, nullable=True)
    if "target_value" in fields:
        cols["target_value"] = ws.number(fields["target_value"], "target_value", nullable=True)
    if "stop_kind" in fields:
        cols["stop_kind"] = ws.choice(fields["stop_kind"], "stop_kind", _STOP_KINDS, nullable=True)
    if "stop_value" in fields:
        cols["stop_value"] = ws.number(fields["stop_value"], "stop_value", nullable=True)
    if "exit_by" in fields:
        cols["exit_by"] = ws.calendar_date(fields["exit_by"], "exit_by", nullable=True)
    for name in ("rationale", "source_ref"):
        if name in fields:
            cols[name] = ws.text(fields[name], name, nullable=True)
    if "source_kind" in fields:
        cols["source_kind"] = ws.choice(fields["source_kind"], "source_kind", _SOURCE_KINDS, nullable=False)
    if "expires_at" in fields:
        cols["expires_at"] = ws.timestamp(fields["expires_at"], "expires_at", nullable=True)
    if "legs" in fields:
        try:
            legs = normalize_plan_legs(ws.list_value(fields["legs"], "legs"))
        except PlanRuleError as e:
            raise WriteInvalid(e.reason) from None
        cols["legs_json"] = json.dumps(legs)
    if "source" in fields:
        items = ws.list_value(fields["source"], "source")
        if any(not isinstance(item, dict) for item in items):
            raise WriteInvalid("source must be a list of objects.")
        cols["source_json"] = json.dumps([dict(item) for item in items])
    return cols


def patch_plan(conn_or_config: Any, strategy_plan_id: int, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change the fields the client sent; return the plan as ``get_plan`` reads it.

    A draft takes any field of ``PLAN_PATCHABLE``. An intended plan (expired or not --
    expiry is not stored) takes ``expires_at`` alone; any other field is WriteConflict
    with the reason, and so is any field on a filled or cancelled plan.

    NOT NULL: ``account_id`` · ``symbol`` (upper-cased) · ``structure_label`` · ``qty`` (>= 1) ·
    ``source_kind``. Nullable (null clears): ``strategy_structure_id`` · ``strategy_opportunity_id`` ·
    ``price_effect`` · ``limit_price`` (>= 0) · ``target_kind`` / ``target_value`` · ``stop_kind`` /
    ``stop_value`` (each pair set or cleared together, checked against the stored half) ·
    ``exit_by`` · ``rationale`` · ``source_ref`` · ``expires_at``. Lists: ``legs`` (validated as on
    create), ``source`` -- replaced whole, ``[]`` empties, null refused.
    Raises WriteInvalid, WriteNotFound, WriteConflict, WriteFailed.
    """
    what = f"plan {strategy_plan_id}"
    fields = ws.check_fields(fields, PLAN_PATCHABLE, "plan")
    cols = _patch_plan_columns(fields)
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT status, target_kind, target_value, stop_kind, stop_value "
                "FROM strategy_plan WHERE strategy_plan_id = %s FOR UPDATE",
                (strategy_plan_id,),
            )
            current = cur.fetchone()
        if current is None:
            raise WriteNotFound(f"No plan {strategy_plan_id}.")
        status = str(current["status"])
        if status == "intended":
            frozen = sorted(k for k in fields if k not in PLAN_PATCHABLE_WHEN_INTENDED)
            if frozen:
                raise WriteConflict(
                    f"This plan is intended, so only its expiry (expires_at) can change, not "
                    f"{', '.join(frozen)}. Cancel it and write a new one, or roll it."
                )
        elif status != "draft":
            raise WriteConflict(f"This plan is {status}, and cannot be edited.")
        merged = {**dict(current), **cols}
        for kind, value, label in (
            ("target_kind", "target_value", "target"),
            ("stop_kind", "stop_value", "stop"),
        ):
            if (merged.get(kind) is None) != (merged.get(value) is None):
                raise WriteInvalid(f"A {label} needs both a kind and a value (or neither).")
        assignments, values = ws.set_clause(cols, jsonb=("legs_json", "source_json"))
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE strategy_plan SET {assignments} WHERE strategy_plan_id = %s",
                [*values, strategy_plan_id],
            )
            if cur.rowcount == 0:
                raise WriteNotFound(f"No plan {strategy_plan_id}.")
        row = _get_plan_on(conn, strategy_plan_id)
        if row is None:
            raise WriteFailed(f"{what} was changed but could not be read back; nothing was saved.")
    return row


def delete_plan_strict(conn_or_config: Any, strategy_plan_id: int) -> Dict[str, Any]:
    """``delete_plan`` with outcomes: ``{"deleted": "hard", "strategy_plan_id"}``, or
    WriteNotFound / WriteConflict (only a draft can be deleted) / WriteFailed."""
    what = f"plan {strategy_plan_id}"
    with ws.write_connection(conn_or_config, what) as conn, ws.write_transaction(conn, what, on_fk="conflict"):
        current = _locked_status(conn, strategy_plan_id)
        if current is None:
            raise WriteNotFound(f"No plan {strategy_plan_id}.")
        if current != "draft":
            raise WriteConflict(f"This plan is {current}; only a draft can be deleted.")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM strategy_plan WHERE strategy_plan_id = %s", (strategy_plan_id,))
            if cur.rowcount == 0:
                raise WriteNotFound(f"No plan {strategy_plan_id}.")
    return {"deleted": "hard", "strategy_plan_id": strategy_plan_id}
