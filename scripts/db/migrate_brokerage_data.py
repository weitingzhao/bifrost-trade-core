#!/usr/bin/env python3
"""Copy brokerage tables from a per-env public schema into bifrost_golden_source.brokerage.*.

Usage:
  .venv/bin/python scripts/db/migrate_brokerage_data.py --config config/config.dev.yaml --source-db bifrost_prod
  .venv/bin/python scripts/db/migrate_brokerage_data.py --config config/config.dev.yaml --source-db bifrost_dev --phase 1
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
os.chdir(_PROJECT_ROOT)

# (source_public_table, dest_brokerage_table, column_list or None = *)
PHASE1 = [
    ("account", "account", None),
    ("account_positions", "positions", None),
    ("contract_quote_live", "contract_quote_live", None),
    ("daemon_open_orders", "open_orders", None),
    ("settings_ib_flex", "settings_flex", None),
]

PHASE2 = [
    ("executions_raw_tws", "executions_raw_tws", None),
    ("executions_raw_flex", "executions_raw_flex", None),
    ("executions_raw_journal", "executions_raw_journal", None),
    ("account_execution_commissions", "commissions", None),
    ("account_transactions", "transactions", None),
]


def _adapt_row(row):
    from psycopg2.extras import Json
    out = []
    for v in row:
        if isinstance(v, dict):
            out.append(Json(v))
        elif isinstance(v, list):
            out.append(Json(v))
        else:
            out.append(v)
    return tuple(out)


def _copy_table(src_cur, dst_cur, src_table: str, dst_table: str) -> int:
    src_cur.execute(f"SELECT * FROM public.{src_table}")
    rows = src_cur.fetchall()
    if not rows:
        return 0
    colnames = [d[0] for d in src_cur.description]
    cols = ", ".join(colnames)
    ph = ", ".join(["%s"] * len(colnames))
    inserted = 0
    for row in rows:
        try:
            dst_cur.execute(
                f"INSERT INTO brokerage.{dst_table} ({cols}) VALUES ({ph}) ON CONFLICT DO NOTHING",
                _adapt_row(row),
            )
            inserted += dst_cur.rowcount
        except Exception as e:
            raise RuntimeError(f"copy {src_table} -> brokerage.{dst_table}: {e}") from e
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/config.dev.yaml")
    parser.add_argument("--source-db", default="bifrost_prod", help="per-env DB to copy FROM")
    parser.add_argument("--phase", choices=("1", "2", "all"), default="all")
    args = parser.parse_args()

    import yaml
    import psycopg2
    from bifrost_core.persistence.postgres.connection import (
        _get_conn_params,
        _get_golden_source_conn_params,
    )

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    src_params = _get_conn_params(config)
    src_params["dbname"] = args.source_db
    src_params["connect_timeout"] = 30
    dst_params = _get_golden_source_conn_params(config)
    dst_params["connect_timeout"] = 30

    tables = []
    if args.phase in ("1", "all"):
        tables.extend(PHASE1)
    if args.phase in ("2", "all"):
        tables.extend(PHASE2)

    print(f"Source: {src_params['host']}/{src_params['dbname']}")
    print(f"Dest:   {dst_params['host']}/{dst_params['dbname']}")

    src = psycopg2.connect(**src_params)
    dst = psycopg2.connect(**dst_params)
    try:
        with src.cursor() as sc, dst.cursor() as dc:
            for src_t, dst_t, _ in tables:
                # Check source exists
                sc.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=%s",
                    (src_t,),
                )
                if not sc.fetchone():
                    print(f"  SKIP {src_t} (missing in source)")
                    continue
                n = _copy_table(sc, dc, src_t, dst_t)
                dst.commit()
                # Fix sequences for bigserial tables
                if dst_t.startswith("executions_raw") or dst_t in ("transactions", "open_orders", "settings_flex"):
                    pk = {
                        "executions_raw_tws": "executions_raw_tws_id",
                        "executions_raw_flex": "executions_raw_flex_id",
                        "executions_raw_journal": "executions_raw_journal_id",
                        "transactions": "account_transactions_id",
                        "open_orders": "id",
                        "settings_flex": "id",
                    }.get(dst_t)
                    if pk:
                        dc.execute(
                            f"""
                            SELECT setval(
                              pg_get_serial_sequence('brokerage.{dst_t}', '{pk}'),
                              COALESCE((SELECT MAX({pk}) FROM brokerage.{dst_t}), 1),
                              true
                            )
                            """
                        )
                        dst.commit()
                print(f"  {src_t} -> brokerage.{dst_t}: {n} rows")
        print("Done.")
        return 0
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    sys.exit(main())
