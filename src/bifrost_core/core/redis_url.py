"""Build redis:// URLs from merged YAML + env (shared by daemon, server, Celery, monitor)."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


def _env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def _yaml(block: Dict[str, Any], key: str) -> Any:
    value = block.get(key)
    return None if value is None or (isinstance(value, str) and not value.strip()) else value


def effective_redis_dict(
    config: Optional[Dict[str, Any]] = None,
    *,
    default_db: int = 0,
) -> Dict[str, Any]:
    """The live bus: ``REDIS_*`` env, then the ``redis`` block, then defaults.

    Env wins over YAML everywhere (debt TD-54, Owner 2026-10-03). This bus had it the other
    way round — YAML first, env as a fallback — while the IB bus let env win, and the Postgres
    builders' docstrings claimed env won while their code let YAML win.

    ``default_db`` is used when neither env nor config sets a db (Celery 1, console 0).
    """
    r = (config or {}).get("redis") or {}
    db_raw = _env("REDIS_DB") or _yaml(r, "db")
    return {
        "host": str(_env("REDIS_HOST") or _yaml(r, "host") or "127.0.0.1").strip(),
        "port": int(_env("REDIS_PORT") or _yaml(r, "port") or 6379),
        "db": int(db_raw) if db_raw is not None and db_raw != "" else default_db,
        "password": str(_env("REDIS_PASSWORD") or _yaml(r, "password") or "").strip(),
        "username": str(_env("REDIS_USERNAME") or _yaml(r, "username") or "").strip(),
    }


def effective_ib_redis_dict(
    config: Optional[Dict[str, Any]] = None,
    *,
    default_db: int = 0,
) -> Dict[str, Any]:
    """The IB bus: per field ``REDIS_IB_*`` env, then ``redis_ib``, then the live bus's value.

    With no IB host anywhere it is the live bus. Each field is resolved on its own, so a
    ``REDIS_HOST`` meant for the live bus can never replace the IB host.
    """
    config = config or {}
    ib = config.get("redis_ib") or {}
    if not (_env("REDIS_IB_HOST") or str(ib.get("host") or "").strip()):
        return effective_redis_dict(config, default_db=default_db)
    live = effective_redis_dict(config, default_db=default_db)
    out: Dict[str, Any] = {}
    for key in ("host", "port", "db", "password", "username"):
        env_value = _env(f"REDIS_IB_{key.upper()}")
        yaml_value = _yaml(ib, key)
        out[key] = env_value if env_value != "" else (yaml_value if yaml_value is not None else live[key])
    out["host"] = str(out["host"]).strip()
    out["port"] = int(out["port"])
    out["db"] = int(out["db"])
    out["password"] = str(out["password"] or "").strip()
    out["username"] = str(out["username"] or "").strip()
    return out


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

