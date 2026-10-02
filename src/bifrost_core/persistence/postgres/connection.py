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


def _get_conn_params(config: dict) -> dict:
    """Build connection params from root postgres config, with env overrides."""
    pg = config.get("postgres", {}) or {}
    db = _pg_block_dbname(pg)
    return {
        "host": pg.get("host") or os.environ.get("PGHOST", "127.0.0.1"),
        "port": int(pg.get("port") or os.environ.get("PGPORT", "5432")),
        "dbname": db or os.environ.get("PGDATABASE", "bifrost"),
        "user": pg.get("user") or os.environ.get("PGUSER", "bifrost"),
        "password": pg.get("password") or os.environ.get("PGPASSWORD", ""),
    }


def _get_golden_source_conn_params(config: dict) -> dict:
    """Build connection params for bifrost_golden_source (brokerage.* writes).

    Reads ``config.golden_source`` with env overrides:
    GOLDEN_SOURCE_HOST / PORT / DATABASE / USER / PASSWORD.
    Falls back to postgres host/port when golden_source host is omitted
    (same CNPG cluster, different database).
    """
    gs = config.get("golden_source", {}) or {}
    pg = config.get("postgres", {}) or {}
    db = _pg_block_dbname(gs)
    return {
        "host": (
            gs.get("host")
            or os.environ.get("GOLDEN_SOURCE_HOST")
            or pg.get("host")
            or os.environ.get("PGHOST", "127.0.0.1")
        ),
        "port": int(
            gs.get("port")
            or os.environ.get("GOLDEN_SOURCE_PORT")
            or pg.get("port")
            or os.environ.get("PGPORT", "5432")
        ),
        "dbname": (
            db
            or os.environ.get("GOLDEN_SOURCE_DATABASE")
            or "bifrost_golden_source"
        ),
        "user": (
            gs.get("user")
            or os.environ.get("GOLDEN_SOURCE_USER")
            or pg.get("user")
            or os.environ.get("PGUSER", "bifrost")
        ),
        "password": (
            gs.get("password")
            or os.environ.get("GOLDEN_SOURCE_PASSWORD")
            or pg.get("password")
            or os.environ.get("PGPASSWORD", "")
        ),
    }


# Public names for the two builders above (TD-20, core 0.34.0). api, worker and Flex import the
# private names today; they keep working, and these are the same objects.
get_conn_params = _get_conn_params
get_golden_source_conn_params = _get_golden_source_conn_params
