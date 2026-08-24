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
    username = (r.get("username") or os.environ.get("REDIS_USERNAME") or "").strip()
    return {"host": host, "port": port, "db": db, "password": password, "username": username}


def effective_ib_redis_dict(
    config: Optional[Dict[str, Any]] = None,
    *,
    default_db: int = 0,
) -> Dict[str, Any]:
    """Normalize IB bus redis — prefers ``redis_ib`` when host is set, else ``redis``."""
    config = config or {}
    ib = config.get("redis_ib") or {}
    if not (ib.get("host") or os.environ.get("REDIS_IB_HOST") or "").strip():
        return effective_redis_dict(config, default_db=default_db)
    base = dict(config.get("redis") or {})
    for key in ("host", "port", "db", "password", "username", "enabled"):
        if key in ib and ib[key] not in (None, ""):
            base[key] = ib[key]
    if (os.environ.get("REDIS_IB_HOST") or "").strip():
        base["host"] = os.environ["REDIS_IB_HOST"].strip()
    if (os.environ.get("REDIS_IB_PORT") or "").strip():
        base["port"] = int(os.environ["REDIS_IB_PORT"])
    if (os.environ.get("REDIS_IB_PASSWORD") or "").strip():
        base["password"] = os.environ["REDIS_IB_PASSWORD"].strip()
    if (os.environ.get("REDIS_IB_USERNAME") or "").strip():
        base["username"] = os.environ["REDIS_IB_USERNAME"].strip()
    if os.environ.get("REDIS_IB_DB", "").strip() != "":
        base["db"] = int(os.environ["REDIS_IB_DB"])
    return effective_redis_dict({"redis": base}, default_db=default_db)


def format_redis_url(effective: Dict[str, Any]) -> str:
    """Build redis:// URL from keys host, port, db, password, username (optional)."""
    host = effective["host"]
    port = int(effective["port"])
    db = int(effective["db"])
    password = (effective.get("password") or "").strip()
    username = (effective.get("username") or "").strip()
    auth = ""
    if username and password:
        auth = f"{username}:{password}@"
    elif password:
        auth = f":{password}@"
    return f"redis://{auth}{host}:{port}/{db}"


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


def ib_redis_url_from_config(config: Dict[str, Any]) -> Optional[str]:
    """Return IB bus redis URL — ``redis_ib`` when configured, else same as ``redis_url_from_config``."""
    ib_cfg = config.get("redis_ib") or {}
    if ib_cfg.get("enabled") is False:
        return None
    has_ib_host = bool(
        (ib_cfg.get("host") or "").strip()
        or (os.environ.get("REDIS_IB_HOST") or "").strip()
    )
    if not has_ib_host:
        return redis_url_from_config(config)
    return format_redis_url(effective_ib_redis_dict(config, default_db=0))


def effective_massive_redis_dict(
    config: Optional[Dict[str, Any]] = None,
    *,
    default_db: int = 0,
) -> Dict[str, Any]:
    """Normalize ``redis_massive`` block — shared Polygon Options WS bus (Plugin data NS)."""
    config = config or {}
    rm = config.get("redis_massive") or {}
    if not (rm.get("host") or os.environ.get("REDIS_MASSIVE_HOST") or "").strip():
        return effective_redis_dict(config, default_db=default_db)
    base = dict(config.get("redis") or {})
    for key in ("host", "port", "db", "password", "username", "enabled"):
        if key in rm and rm[key] not in (None, ""):
            base[key] = rm[key]
    if (os.environ.get("REDIS_MASSIVE_HOST") or "").strip():
        base["host"] = os.environ["REDIS_MASSIVE_HOST"].strip()
    if (os.environ.get("REDIS_MASSIVE_PORT") or "").strip():
        base["port"] = int(os.environ["REDIS_MASSIVE_PORT"])
    if (os.environ.get("REDIS_MASSIVE_PASSWORD") or "").strip():
        base["password"] = os.environ["REDIS_MASSIVE_PASSWORD"].strip()
    if (os.environ.get("REDIS_MASSIVE_USERNAME") or "").strip():
        base["username"] = os.environ["REDIS_MASSIVE_USERNAME"].strip()
    if os.environ.get("REDIS_MASSIVE_DB", "").strip() != "":
        base["db"] = int(os.environ["REDIS_MASSIVE_DB"])
    return effective_redis_dict({"redis": base}, default_db=default_db)


def massive_redis_url_from_config(config: Dict[str, Any]) -> Optional[str]:
    """Return Plugin redis-massive URL when ``redis_massive`` host is configured; else fall back to ``redis``."""
    rm_cfg = config.get("redis_massive") or {}
    if rm_cfg.get("enabled") is False:
        return None
    has_host = bool(
        (rm_cfg.get("host") or "").strip()
        or (os.environ.get("REDIS_MASSIVE_HOST") or "").strip()
    )
    if not has_host:
        return redis_url_from_config(config)
    return format_redis_url(effective_massive_redis_dict(config, default_db=0))

