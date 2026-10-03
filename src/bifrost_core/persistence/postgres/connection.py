"""Shared PostgreSQL connection helpers for persistence, scripts, and servers.

``release_pg_locks_for_tables`` (pg_terminate_backend on lock holders) was removed in
core 0.35.0 (TD-45); nothing may terminate other backends to get its way.

Config shape and env vars (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD) match docs/DATABASE.md §1.
"""

import os


def _pg_block_dbname(pg: dict) -> str | None:
    """Extract database name from a postgres-like config block."""
    db = pg.get("database") or pg.get("Database") or pg.get("db") or pg.get("dbname")
    if not db and pg:
        for k, v in pg.items():
            if (
                k
                and isinstance(v, str)
                and v.strip()
                and k.strip().lower() in ("database", "db", "dbname")
            ):
                db = v.strip()
                break
    return db


def _env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def _yaml(block: dict, key: str):
    value = block.get(key)
    return None if value is None or (isinstance(value, str) and not value.strip()) else value


def _get_conn_params(config: dict) -> dict:
    """The Trade database: ``PG*`` env, then the ``postgres`` block, then defaults.

    Env wins over YAML (debt TD-54, Owner 2026-10-03). The docstring said so before; the
    code let YAML win, so an env value could never override a file. In K3s the overlay's
    password fields are empty and the Secret supplies them either way.
    """
    pg = config.get("postgres", {}) or {}
    return {
        "host": _env("PGHOST") or _yaml(pg, "host") or "127.0.0.1",
        "port": int(_env("PGPORT") or _yaml(pg, "port") or 5432),
        "dbname": _env("PGDATABASE") or _pg_block_dbname(pg) or "bifrost",
        "user": _env("PGUSER") or _yaml(pg, "user") or "bifrost",
        "password": _env("PGPASSWORD") or _yaml(pg, "password") or "",
    }


def _get_golden_source_conn_params(config: dict) -> dict:
    """bifrost_golden_source: per field ``GOLDEN_SOURCE_*`` env, then ``golden_source``,
    then the Trade database's value (same CNPG cluster, different database).

    Env wins over YAML (TD-54): ``GOLDEN_SOURCE_USER`` used to lose to a ``user:`` line in the
    overlay. The database name never falls back to the Trade database.
    """
    gs = config.get("golden_source", {}) or {}
    trade = _get_conn_params(config)
    return {
        "host": _env("GOLDEN_SOURCE_HOST") or _yaml(gs, "host") or trade["host"],
        "port": int(_env("GOLDEN_SOURCE_PORT") or _yaml(gs, "port") or trade["port"]),
        "dbname": _env("GOLDEN_SOURCE_DATABASE") or _pg_block_dbname(gs) or "bifrost_golden_source",
        "user": _env("GOLDEN_SOURCE_USER") or _yaml(gs, "user") or trade["user"],
        "password": _env("GOLDEN_SOURCE_PASSWORD") or _yaml(gs, "password") or trade["password"],
    }


# Public names for the two builders above (TD-20, core 0.34.0). api, worker and Flex import the
# private names today; they keep working, and these are the same objects.
get_conn_params = _get_conn_params
get_golden_source_conn_params = _get_golden_source_conn_params
