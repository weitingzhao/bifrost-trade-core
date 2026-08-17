#!/usr/bin/env python3
"""Initialize brokerage Golden Source schema + optional per-env FDW.

Usage:
  python scripts/db/db_init_brokerage.py              # DDL on golden_source only
  python scripts/db/db_init_brokerage.py --with-fdw   # also FDW into per-env DB
  python scripts/db/db_init_brokerage.py --roles-sql  # print elevated role SQL
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.chdir(_PROJECT_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description="Init brokerage Golden Source schema")
    parser.add_argument("--config", default=None, metavar="PATH")
    parser.add_argument(
        "--with-fdw",
        action="store_true",
        help="Also set up postgres_fdw foreign tables in the per-env database",
    )
    parser.add_argument(
        "--roles-sql",
        action="store_true",
        help="Print elevated SQL to create brokerage_writer/reader roles and exit",
    )
    args, argv_remainder = parser.parse_known_args(sys.argv[1:])

    from bifrost_core.persistence.postgres.brokerage_ddl import (
        apply_brokerage_roles_sql,
        ensure_brokerage_schema,
        setup_fdw_foreign_tables,
    )

    if args.roles_sql:
        print(apply_brokerage_roles_sql())
        return 0

    if args.config:
        config_path = args.config
        if not os.path.isabs(config_path):
            config_path = str(_PROJECT_ROOT / config_path)
        config_path = str(Path(config_path).resolve())
    else:
        from bifrost_core.config.startup import resolve_startup_config_path

        config_path, _ = resolve_startup_config_path(str(_PROJECT_ROOT), argv_remainder)

    if not Path(config_path).exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        return 1

    import yaml
    import psycopg2
    from bifrost_core.persistence.postgres.connection import (
        _get_conn_params,
        _get_golden_source_conn_params,
    )

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    gs_params = _get_golden_source_conn_params(config)
    gs_params["connect_timeout"] = 15
    print(f"Golden Source: {gs_params['user']}@{gs_params['host']}:{gs_params['port']}/{gs_params['dbname']}")

    conn = psycopg2.connect(**gs_params)
    try:
        ensure_brokerage_schema(conn, log=print)
        print("Brokerage schema ready.")
    finally:
        conn.close()

    if args.with_fdw:
        env_params = _get_conn_params(config)
        env_params["connect_timeout"] = 15
        print(
            f"FDW into: {env_params['user']}@{env_params['host']}:{env_params['port']}/{env_params['dbname']}"
        )
        gs = config.get("golden_source") or {}
        # FDW remote role preferably brokerage_reader
        fdw_params = dict(gs_params)
        fdw_params["user"] = gs.get("fdw_user") or "brokerage_reader"
        fdw_params["password"] = (
            gs.get("fdw_password") or gs.get("password") or fdw_params.get("password") or ""
        )
        env_conn = psycopg2.connect(**env_params)
        try:
            setup_fdw_foreign_tables(
                env_conn,
                fdw_params,
                local_user=str(env_params["user"]),
                log=print,
            )
            print("FDW foreign tables ready.")
        finally:
            env_conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
