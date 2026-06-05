"""Parse strategy API query/body fields (no FastAPI). Failures raise ValueError for HTTP 400 mapping."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional


def parse_strategy_instance_ids_csv(value: Optional[str]) -> Optional[List[int]]:
    """Parse comma-separated positive integer IDs; dedupe preserving order. None/empty => no filter."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    out: List[int] = []
    seen: set = set()
    for part in s.split(","):
        p = part.strip()
        if not p:
            continue
        try:
            n = int(p, 10)
        except ValueError as e:
            raise ValueError(f"Invalid strategy instance id: {p!r}") from e
        if n <= 0:
            raise ValueError("strategy_instance_ids must be positive integers")
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out if out else None


def parse_optional_timestamp(value: Any, field_name: str) -> Optional[float]:
    """Parse optional timestamp from payload: ISO 8601 or Unix seconds. Returns float or None."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            s = value.strip()
            if s.replace(".", "").replace("-", "").replace(":", "").replace("Z", "").isdigit():
                return float(s)
            normalized = s.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(normalized).timestamp()
            except ValueError:
                if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                    return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        pass
    raise ValueError(f"{field_name} must be ISO 8601 or Unix timestamp")


def parse_opened_at_to_unix(opened_at: Any) -> float:
    """Parse opened_at from create-instance body: numeric string or ISO 8601."""
    opened_at_val: Any = opened_at
    try:
        if isinstance(opened_at_val, str) and opened_at_val.strip().replace(".", "").replace("-", "").replace(":", "", 1).isdigit():
            return float(opened_at_val.strip())
        if isinstance(opened_at_val, str):
            return datetime.fromisoformat(opened_at_val.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError) as exc:
        raise ValueError("opened_at must be ISO 8601 or Unix timestamp") from exc
    raise ValueError("opened_at must be ISO 8601 or Unix timestamp")
