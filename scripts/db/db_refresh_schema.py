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
    EXPECTED_TABLES_BY_CATEGORY,
    TABLE_TO_CATEGORY,
    _c,
    _color_enabled,
    _log_table,
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
    dbname = params["dbname"]

    _progress(f"Using config: {config_path}", no_color)
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
