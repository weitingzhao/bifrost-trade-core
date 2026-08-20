#!/usr/bin/env python3
"""Refresh PostgreSQL schema via bifrost_core.persistence.postgres.ddl."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.chdir(_PROJECT_ROOT)

# Re-use engine reporting tables list (canonical expected objects)
from scripts.db._schema_report import (  # noqa: E402
    CATEGORY_ORDER,
    TABLE_TO_CATEGORY,
    _c,
    _progress,
    _step,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh PostgreSQL schema (bifrost_core DDL).")
    parser.add_argument("--config", default=None, metavar="PATH")
    parser.add_argument("--no-color", action="store_true")
    args, argv_remainder = parser.parse_known_args(sys.argv[1:])
    no_color = args.no_color

    if args.config:
        config_path = args.config
        if not os.path.isabs(config_path):
            config_path = str(_PROJECT_ROOT / config_path)
        config_path = str(Path(config_path).resolve())
    else:
        from bifrost_core.config.startup import resolve_startup_config_path

        config_path, _ = resolve_startup_config_path(str(_PROJECT_ROOT), argv_remainder)

    if not Path(config_path).exists():
        print(f"{_c(no_color, '31', 'Config not found:')} {config_path}", file=sys.stderr)
        return 1

    try:
        import yaml
        import psycopg2
        from bifrost_core.persistence.postgres.ddl import _ensure_tables
        from bifrost_core.persistence.postgres.connection import _get_conn_params
    except ImportError as e:
        print(f"Missing dependency: {e}", file=sys.stderr)
        return 1

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    pg = config.get("postgres") or {}
    if not pg and not os.environ.get("PGHOST"):
        print("postgres or PGHOST required.", file=sys.stderr)
        return 1
    params = _get_conn_params(config)
    params["connect_timeout"] = 10

    _progress(f"Using config: {config_path} ({params['dbname']})", no_color)
    conn = psycopg2.connect(**params)
    try:
        with conn.cursor() as cur:
            cur.execute("SET lock_timeout = '20s'")
            cur.execute("SET statement_timeout = '60s'")
        conn.commit()

        tables_by_category = {c: [] for c in CATEGORY_ORDER}

        def log_table_by_category(table_name: str, purpose: str) -> None:
            cat = TABLE_TO_CATEGORY.get(table_name, "other")
            tables_by_category.setdefault(cat, []).append((table_name, purpose))

        _ensure_tables(conn, log=lambda m: _step(m, no_color), log_table=log_table_by_category)
        conn.commit()
        _progress("Schema refresh complete.", no_color)

        gs_cfg = config.get("golden_source") or {}
        if not gs_cfg and not os.environ.get("GOLDEN_SOURCE_HOST"):
            _progress("golden_source not configured; skipping brokerage DDL / FDW.", no_color)
            return 0

        from bifrost_core.persistence.postgres.brokerage_ddl import (
            ensure_brokerage_schema,
            setup_fdw_foreign_tables,
            setup_fdw_market_tables,
        )
        from bifrost_core.persistence.postgres.connection import _get_golden_source_conn_params

        gs_params = _get_golden_source_conn_params(config)
        gs_params["connect_timeout"] = 15
        _progress(
            f"Brokerage Golden Source: {gs_params['user']}@{gs_params['host']}:"
            f"{gs_params['port']}/{gs_params['dbname']}",
            no_color,
        )
        gs_conn = psycopg2.connect(**gs_params)
        try:
            ensure_brokerage_schema(gs_conn, log=lambda m: _step(f"brokerage {m}", no_color))
            _progress("Brokerage schema ready.", no_color)
        finally:
            gs_conn.close()

        fdw_params = dict(gs_params)
        fdw_params["user"] = gs_cfg.get("fdw_user") or "brokerage_reader"
        fdw_params["password"] = (
            gs_cfg.get("fdw_password") or gs_cfg.get("password") or fdw_params.get("password") or ""
        )
        try:
            setup_fdw_foreign_tables(
                conn,
                fdw_params,
                local_user=str(params["user"]),
                log=lambda m: _step(f"fdw {m}", no_color),
            )
            setup_fdw_market_tables(
                conn,
                local_user=str(params["user"]),
                log=lambda m: _step(f"fdw market {m}", no_color),
            )
            _progress("FDW foreign tables ready.", no_color)
        except Exception as e:
            print(
                f"FDW setup skipped (needs CREATE EXTENSION / SERVER privilege): {e}",
                file=sys.stderr,
            )
        return 0
    except Exception as e:
        print(f"Schema refresh failed: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
