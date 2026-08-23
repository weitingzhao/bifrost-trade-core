#!/usr/bin/env python3
"""Re-import Golden Source FDW after raw_broker / raw_market schema rename.

Per-env DBs keep local ``brokerage`` / ``market`` schemas; remote physical tables
are ``raw_broker.*`` and ``raw_market.*`` on bifrost_golden_source.

Usage:
  PGPASSWORD=... GOLDEN_SOURCE_PASSWORD=... python scripts/db/fdw_reimport_pipeline_schemas.py \\
    --config /path/to/config.dev.yaml

  # All Trade env DBs (dev/stg/prod) from one config overlay:
  python scripts/db/fdw_reimport_pipeline_schemas.py --config config.yaml \\
    --env-database bifrost_dev --env-database bifrost_stg --env-database bifrost_prod
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
    parser = argparse.ArgumentParser(description="Re-import FDW for raw_broker/raw_market")
    parser.add_argument("--config", required=True, metavar="PATH")
    parser.add_argument(
        "--env-database",
        action="append",
        default=None,
        metavar="DB",
        help="Per-env database name (default: postgres.database from config)",
    )
    args = parser.parse_args()

    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = str(_PROJECT_ROOT / config_path)
    if not Path(config_path).exists():
        print(f"Config not found: {config_path}", file=sys.stderr)
        return 1

    import yaml
    import psycopg2
    from bifrost_core.persistence.postgres.brokerage_ddl import (
        setup_fdw_foreign_tables,
        setup_fdw_market_tables,
    )
    from bifrost_core.persistence.postgres.connection import (
        _get_conn_params,
        _get_golden_source_conn_params,
    )

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    gs_params = _get_golden_source_conn_params(config)
    gs_params["connect_timeout"] = 15
    gs = config.get("golden_source") or {}
    fdw_params = dict(gs_params)
    fdw_params["user"] = gs.get("fdw_user") or "brokerage_reader"
    fdw_params["password"] = (
        gs.get("fdw_password")
        or os.environ.get("BROKERAGE_READER_PASSWORD")
        or gs.get("password")
        or os.environ.get("GOLDEN_SOURCE_PASSWORD")
        or fdw_params.get("password")
        or ""
    )

    env_databases = args.env_database or [str(_get_conn_params(config)["dbname"])]

    # Local runs against CNPG use NodePort; in-cluster configs may use *.svc.cluster.local.
    lan_host = os.environ.get("BIFROST_PG_LAN_HOST", "192.168.10.73")
    lan_port = int(os.environ.get("BIFROST_PG_LAN_PORT", "30432"))

    for dbname in env_databases:
        env_params = _get_conn_params(config)
        env_params["dbname"] = dbname
        env_params["connect_timeout"] = 15
        if ".svc.cluster.local" in str(env_params.get("host") or ""):
            env_params["host"] = lan_host
            env_params["port"] = lan_port
        if not env_params.get("password"):
            env_params["password"] = os.environ.get("PGPASSWORD", "")

        print(
            f"FDW reimport: {env_params['user']}@{env_params['host']}:"
            f"{env_params['port']}/{dbname}"
        )
        env_conn = psycopg2.connect(**env_params)
        try:
            setup_fdw_foreign_tables(
                env_conn,
                fdw_params,
                local_user=str(env_params["user"]),
                skip_server_admin=True,
                log=print,
            )
            setup_fdw_market_tables(
                env_conn,
                local_user=str(env_params["user"]),
                log=print,
            )
            print(f"OK: {dbname}")
        finally:
            env_conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
