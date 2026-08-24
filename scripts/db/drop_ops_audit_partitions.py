#!/usr/bin/env python3
"""Drop ops_audit_log monthly partitions older than N months (Wave 4 retention).

Usage:
  python scripts/db/drop_ops_audit_partitions.py [--months 3]

Requires postgres config (same as make db-init). Celery beat was retired;
retention also runs opportunistically inside _ensure_tables().
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from bifrost_core.persistence.postgres.connection import _get_conn_params  # noqa: E402
from bifrost_core.persistence.postgres.ddl import (  # noqa: E402
    drop_ops_audit_log_partitions_older_than,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=3, help="Keep last N months (default 3)")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "config.yaml",
        help="Path to config.yaml",
    )
    args = parser.parse_args()
    if not args.config.exists():
        alt = ROOT / "config" / "config.yaml.example"
        if not alt.exists():
            print(f"config not found: {args.config}", file=sys.stderr)
            return 1
        args.config = alt

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    import psycopg2

    conn = psycopg2.connect(**_get_conn_params(config))
    try:
        n = drop_ops_audit_log_partitions_older_than(conn, cutoff_months=args.months)
        print(f"dropped {n} ops_audit_log partition(s) older than {args.months} month(s)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
