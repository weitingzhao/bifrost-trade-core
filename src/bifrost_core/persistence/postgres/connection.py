"""Shared PostgreSQL connection and lock helpers for persistence, scripts, and servers.

Config shape and env vars (PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD) match docs/DATABASE.md §1.
"""

import logging
import os
from typing import List, Tuple

import psycopg2

logger = logging.getLogger(__name__)

# Table(s) to auto-release locks on when daemon hits lock timeout (e.g. after crash restart)
_DAEMON_LOCK_TABLES: Tuple[str, ...] = ("daemon_heartbeat", "daemon_run_status")


def _is_lock_timeout_error(e: Exception) -> bool:
    """True if exception is due to lock timeout (55P03 or message)."""
    if getattr(e, "pgcode", None) == "55P03":
        return True
    msg = str(e).lower()
    return "lock timeout" in msg or "canceling statement due to lock timeout" in msg


def release_pg_locks_for_tables(
    config: dict,
    tables: Tuple[str, ...] = _DAEMON_LOCK_TABLES,
) -> int:
    """Open a new connection, find backends holding or waiting for locks on the given
    table names, terminate them (pg_terminate_backend), and return the number terminated.
    Used when the daemon hits lock timeout on daemon_heartbeat or daemon_run_status after crash/restart.
    """
    params = _get_conn_params(config)
    params["connect_timeout"] = 10
    try:
        conn = psycopg2.connect(**params)
    except Exception as e:
        logger.warning("release_pg_locks_for_tables: connect failed: %s", e)
        return 0
    my_pid = conn.get_backend_pid()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT l.pid
                FROM pg_locks l
                JOIN pg_class c ON l.relation = c.oid
                JOIN pg_stat_activity a ON l.pid = a.pid
                WHERE c.relname = ANY(%s)
                  AND l.pid != %s
                """,
                (list(tables), my_pid),
            )
            pids: List[int] = [r[0] for r in cur.fetchall()]
    except Exception as e:
        logger.warning("release_pg_locks_for_tables: query failed: %s", e)
        conn.close()
        return 0
    terminated = 0
    for pid in pids:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
                if cur.fetchone()[0]:
                    terminated += 1
                    logger.info("Terminated backend pid=%s (lock on %s)", pid, tables)
        except Exception as e:
            logger.debug("Failed to terminate pid=%s: %s", pid, e)
    conn.close()
    return terminated


def _get_conn_params(config: dict) -> dict:
    """Build connection params from root postgres config, with env overrides."""
    pg = config.get("postgres", {}) or {}
    # Database: support database, Database, db, or any key that lower() in ("database", "db")
    db = pg.get("database") or pg.get("Database") or pg.get("db")
    if not db and pg:
        for k, v in pg.items():
            if (
                k
                and isinstance(v, str)
                and v.strip()
                and k.strip().lower() in ("database", "db")
            ):
                db = v.strip()
                break
    return {
        "host": pg.get("host") or os.environ.get("PGHOST", "127.0.0.1"),
        "port": int(pg.get("port") or os.environ.get("PGPORT", "5432")),
        "dbname": db or os.environ.get("PGDATABASE", "bifrost"),
        "user": pg.get("user") or os.environ.get("PGUSER", "bifrost"),
        "password": pg.get("password") or os.environ.get("PGPASSWORD", ""),
    }
