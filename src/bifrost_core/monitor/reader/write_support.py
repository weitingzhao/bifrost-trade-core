"""Shared plumbing for the TD-15 writers: ``patch_*`` and ``*_strict`` (core 0.33.0).

The older writers answer ``True`` / ``False`` / ``None``, so a missing row, a row
in use, bad input and a dead database all reach the API as the same ``False``.
The writers built on this module return what they wrote and raise one of the
``Write*`` outcomes in ``errors`` instead:

- ``write_connection`` -- a status config (dict) opens and closes its own
  connection, and a config that is not Postgres, or a connect that fails, is
  ``WriteFailed``; anything else is taken as a live connection the caller owns.
- ``write_transaction`` -- commits on success; on any exception rolls back and
  re-raises it as a ``Write*`` (``as_write_error``).
- ``check_fields`` and the value helpers -- the PATCH input rules, one place:

  * ``fields`` holds exactly what the client sent. Empty is ``WriteInvalid``;
    a key the resource does not patch is ``WriteInvalid`` naming it.
  * An explicit ``None`` clears a nullable column; on a NOT NULL column it is
    ``WriteInvalid``.
  * Text is trimmed. A blank string is never turned into NULL: on a nullable
    column it is ``WriteInvalid`` ("send null to clear it"), on a NOT NULL
    column it is ``WriteInvalid`` ("is required").
  * Numbers, ids, booleans, dates and timestamps are type-checked, not coerced
    from junk (``True`` is not the integer 1).
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import psycopg2
import psycopg2.errors

from bifrost_core.monitor.reader.errors import (
    WriteConflict,
    WriteError,
    WriteFailed,
    WriteInvalid,
    WriteNotFound,
)
from bifrost_core.persistence.postgres.connection import (
    _get_conn_params,
    _get_golden_source_conn_params,
)

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 10


def is_postgres_config(status_config: Any) -> bool:
    """The same test every writer's ``_conn_from_config`` makes."""
    return isinstance(status_config, dict) and (
        status_config.get("sink") == "postgres" or bool(status_config.get("postgres"))
    )


def connect(params: Dict[str, Any], golden: bool = False) -> Any:
    """Open one connection. A seam: tests replace it with a fake."""
    return psycopg2.connect(**params)


def open_conn(status_config: Dict[str, Any], *, golden: bool = False) -> Any:
    """Open a connection from a status config, with a connect timeout; raises on failure.

    The per-env database, or with ``golden=True`` the Golden Source. Every reader and
    writer that opens its own connection from a status config goes through here, so a
    host that does not answer fails after ``_CONNECT_TIMEOUT_S`` instead of hanging
    (TD-46, 0.33.1). The caller owns the connection: commit / rollback / close as before.
    """
    params = (_get_golden_source_conn_params if golden else _get_conn_params)(status_config)
    return connect({**params, "connect_timeout": _CONNECT_TIMEOUT_S}, golden=golden)


def conn_from_config(
    status_config: Optional[Dict[str, Any]], what: str, *, log: Optional[logging.Logger] = None
) -> Any:
    """A connection to the per-env database, or None.

    The shared body of every module's ``_conn_from_config``: None when the config is
    empty or not Postgres, and None (logged as a warning) when the connect fails.
    """
    if not status_config or (
        status_config.get("sink") != "postgres" and not status_config.get("postgres")
    ):
        return None
    try:
        return open_conn(status_config)
    except Exception as e:
        (log or logger).warning("%s connect failed: %s", what, e)
        return None


@contextmanager
def write_connection(conn_or_config: Any, what: str, *, golden: bool = False) -> Iterator[Any]:
    """Yield a connection for one write.

    A dict is a status config: a connection is opened (to the per-env database,
    or with ``golden=True`` to the Golden Source) and closed afterwards. Anything
    else is a live connection the caller owns and keeps open.
    """
    if conn_or_config is None:
        raise WriteFailed(f"Cannot write {what}: Postgres is not configured.", unavailable=True)
    if not isinstance(conn_or_config, dict):
        yield conn_or_config
        return
    if not is_postgres_config(conn_or_config):
        raise WriteFailed(f"Cannot write {what}: Postgres is not configured.", unavailable=True)
    store = "the Golden Source" if golden else "the database"
    try:
        conn = open_conn(conn_or_config, golden=golden)
    except Exception as e:
        logger.warning("write %s: connect to %s failed: %s", what, store, e)
        raise WriteFailed(f"Cannot write {what}: {store} is unreachable.", unavailable=True) from e
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - close failure path
            pass


def rollback_quietly(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:  # pragma: no cover - rollback failure path
        pass


def as_write_error(exc: BaseException, what: str, *, on_fk: str = "invalid") -> WriteError:
    """Translate an exception from a write into its ``Write*`` outcome.

    ``on_fk`` says what a foreign-key violation means here: ``"invalid"`` (an
    UPDATE / INSERT named a row that does not exist), ``"conflict"`` (a DELETE
    hit a row that is still referenced) or ``"not_found"`` (the parent row the
    write hangs off is gone).
    """
    if isinstance(exc, WriteError):
        return exc
    detail = _pg_detail(exc)
    if isinstance(exc, psycopg2.errors.UniqueViolation):
        return WriteConflict(f"Cannot write {what}: that value is already used{detail}.")
    if isinstance(exc, psycopg2.errors.ForeignKeyViolation):
        if on_fk == "conflict":
            return WriteConflict(f"Cannot delete {what}: other rows still reference it{detail}.")
        if on_fk == "not_found":
            return WriteNotFound(f"Cannot write {what}: it does not exist.")
        return WriteInvalid(f"Cannot write {what}: a referenced row does not exist{detail}.")
    if isinstance(exc, psycopg2.errors.NotNullViolation):
        return WriteInvalid(f"Cannot write {what}: a required field is missing{detail}.")
    if isinstance(exc, psycopg2.errors.CheckViolation):
        return WriteInvalid(f"Cannot write {what}: a value is out of range{detail}.")
    if isinstance(exc, psycopg2.DataError):
        return WriteInvalid(f"Cannot write {what}: a value has the wrong form{detail}.")
    logger.warning("write %s failed: %s", what, exc)
    return WriteFailed(f"Cannot write {what}: the database write failed.")


def _pg_detail(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    if diag is None:
        return ""
    for attr in ("column_name", "constraint_name"):
        value = getattr(diag, attr, None)
        if value:
            return f" ({value})"
    return ""


@contextmanager
def write_transaction(conn: Any, what: str, *, on_fk: str = "invalid") -> Iterator[Any]:
    """Run the body as one transaction: commit at the end, roll back and raise ``Write*`` on error."""
    try:
        yield conn
        conn.commit()
    except Exception as e:
        rollback_quietly(conn)
        err = as_write_error(e, what, on_fk=on_fk)
        if err is e:
            raise
        raise err from e


def check_fields(fields: Any, allowed: Sequence[str], what: str) -> Dict[str, Any]:
    """The PATCH envelope rules: an object, not empty, only patchable keys."""
    if not isinstance(fields, dict):
        raise WriteInvalid(f"The {what} fields must be an object.")
    if not fields:
        raise WriteInvalid(f"Nothing to change: send at least one {what} field.")
    unknown = sorted(str(k) for k in fields if k not in allowed)
    if unknown:
        raise WriteInvalid(
            f"Unknown {what} field{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}. "
            f"Patchable: {', '.join(allowed)}."
        )
    return dict(fields)


def text(value: Any, name: str, *, nullable: bool, max_len: Optional[int] = None) -> Optional[str]:
    """Trimmed text. None clears when nullable; blank is refused either way."""
    if value is None:
        if nullable:
            return None
        raise WriteInvalid(f"{name} is required.")
    if not isinstance(value, str):
        raise WriteInvalid(f"{name} must be text.")
    out = value.strip()
    if not out:
        if nullable:
            raise WriteInvalid(f"{name} is blank; send null to clear it.")
        raise WriteInvalid(f"{name} is required.")
    if max_len is not None and len(out) > max_len:
        raise WriteInvalid(f"{name} is longer than {max_len} characters.")
    return out


def integer(value: Any, name: str, *, nullable: bool, minimum: Optional[int] = None) -> Optional[int]:
    """A whole number (an int, or a string of digits). Not a bool, not 1.5."""
    if value is None:
        if nullable:
            return None
        raise WriteInvalid(f"{name} is required.")
    if isinstance(value, bool):
        raise WriteInvalid(f"{name} must be a whole number.")
    if isinstance(value, int):
        out = value
    elif isinstance(value, float) and value.is_integer():
        out = int(value)
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        out = int(value.strip())
    else:
        raise WriteInvalid(f"{name} must be a whole number.")
    if minimum is not None and out < minimum:
        raise WriteInvalid(f"{name} must be {minimum} or more.")
    return out


def row_id(value: Any, name: str, *, nullable: bool) -> Optional[int]:
    """A reference to another row's id: a whole number of 1 or more."""
    return integer(value, name, nullable=nullable, minimum=1)


def number(value: Any, name: str, *, nullable: bool, minimum: Optional[float] = None) -> Optional[float]:
    """A finite number. Not a bool, not a string."""
    if value is None:
        if nullable:
            return None
        raise WriteInvalid(f"{name} is required.")
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise WriteInvalid(f"{name} must be a number.")
    out = float(value)
    if not math.isfinite(out):
        raise WriteInvalid(f"{name} must be a finite number.")
    if minimum is not None and out < minimum:
        raise WriteInvalid(f"{name} must be {minimum:g} or more.")
    return out


def boolean(value: Any, name: str) -> bool:
    """true or false; a boolean column here is never cleared to NULL."""
    if not isinstance(value, bool):
        raise WriteInvalid(f"{name} must be true or false.")
    return value


def choice(value: Any, name: str, options: Iterable[str], *, nullable: bool) -> Optional[str]:
    """One of a fixed set of codes, as stored (no case folding)."""
    out = text(value, name, nullable=nullable)
    if out is None:
        return None
    opts = tuple(options)
    if out not in opts:
        raise WriteInvalid(f"{name} must be one of {', '.join(opts)}.")
    return out


def timestamp(value: Any, name: str, *, nullable: bool) -> Optional[datetime]:
    """A moment: a datetime (naive reads as UTC), Unix seconds, or an ISO 8601 string."""
    if value is None:
        if nullable:
            return None
        raise WriteInvalid(f"{name} is required.")
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, bool):
        raise WriteInvalid(f"{name} must be a timestamp.")
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise WriteInvalid(f"{name} is not a valid Unix time.") from None
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            raise WriteInvalid(f"{name} must be an ISO 8601 timestamp or Unix seconds.") from None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    raise WriteInvalid(f"{name} must be a timestamp.")


def calendar_date(value: Any, name: str, *, nullable: bool) -> Optional[date]:
    """A date: a ``date`` or ``YYYY-MM-DD``."""
    if value is None:
        if nullable:
            return None
        raise WriteInvalid(f"{name} is required.")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            pass
    raise WriteInvalid(f"{name} must be a date (YYYY-MM-DD).")


def list_value(value: Any, name: str) -> List[Any]:
    """A list a PATCH replaces whole. None is refused: send [] to empty it."""
    if value is None:
        raise WriteInvalid(f"{name} cannot be null; send [] to empty it.")
    if not isinstance(value, list):
        raise WriteInvalid(f"{name} must be a list.")
    return value


def set_clause(columns: Dict[str, Any], *, jsonb: Iterable[str] = (), touch: bool = True) -> Tuple[str, List[Any]]:
    """``col = %s, …`` (``%s::jsonb`` for the named columns) and its values, ``updated_at = now()`` last."""
    as_json = set(jsonb)
    parts = [f"{col} = %s::jsonb" if col in as_json else f"{col} = %s" for col in columns]
    if touch:
        parts.append("updated_at = now()")
    return ", ".join(parts), list(columns.values())


def name_list(names: Sequence[str], limit: int = 5) -> str:
    """'A, B and C' -- at most ``limit`` names, then 'and N more'."""
    shown = list(names[:limit])
    rest = len(names) - len(shown)
    if rest > 0:
        return f"{', '.join(shown)} and {rest} more"
    if len(shown) <= 1:
        return "".join(shown)
    return f"{', '.join(shown[:-1])} and {shown[-1]}"


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


__all__ = [
    "as_write_error",
    "boolean",
    "calendar_date",
    "check_fields",
    "choice",
    "connect",
    "integer",
    "is_postgres_config",
    "list_value",
    "name_list",
    "number",
    "plural",
    "rollback_quietly",
    "row_id",
    "set_clause",
    "text",
    "timestamp",
    "write_connection",
    "write_transaction",
]
