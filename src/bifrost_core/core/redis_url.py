"""Build redis:// URLs from merged YAML + env (shared by daemon, server, Celery, monitor)."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


def effective_redis_dict(
    config: Optional[Dict[str, Any]] = None,
    *,
    default_db: int = 0,
) -> Dict[str, Any]:
    """Normalize redis block from merged config with REDIS_* env fallbacks.

    ``default_db`` is used when neither config nor REDIS_DB is set (e.g. Celery uses 1, console uses 0).
    """
    config = config or {}
    r = config.get("redis") or {}
    host = (r.get("host") or os.environ.get("REDIS_HOST") or "127.0.0.1").strip()
    port = int(r.get("port") or os.environ.get("REDIS_PORT") or 6379)
    db_raw = r.get("db")
    if db_raw is not None and db_raw != "":
        db = int(db_raw)
    elif os.environ.get("REDIS_DB", "").strip() != "":
        db = int(os.environ["REDIS_DB"])
    else:
        db = default_db
    password = (r.get("password") or os.environ.get("REDIS_PASSWORD") or "").strip()
    return {"host": host, "port": port, "db": db, "password": password}


def format_redis_url(effective: Dict[str, Any]) -> str:
    """Build redis:// URL from keys host, port, db, password (optional)."""
    host = effective["host"]
    port = int(effective["port"])
    db = int(effective["db"])
    password = (effective.get("password") or "").strip()
    if password:
        return f"redis://:{password}@{host}:{port}/{db}"
    return f"redis://{host}:{port}/{db}"


def redis_url_from_config(config: Dict[str, Any]) -> Optional[str]:
    """Return redis URL if redis or realtime is enabled; else None.

    Uses the same host/port/db/password rules as console log URLs (env fallbacks, default db 0).
    """
    rc = config.get("redis") or {}
    realtime_cfg = config.get("realtime") or {}
    enabled = bool(rc.get("enabled", False) or realtime_cfg.get("enabled", False))
    if not enabled:
        return None
    return format_redis_url(effective_redis_dict(config, default_db=0))
