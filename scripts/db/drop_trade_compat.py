#!/usr/bin/env python3
"""Naming R4: print the one-transaction drop of R3's one-version objects for one env database.

Prints SQL only (no connection, no credentials); pipe it into psql on that env's database,
connected as postgres (the SQL switches to bifrost, the owner of every object it drops):

  python scripts/db/drop_trade_compat.py --env dev | psql -X -v ON_ERROR_STOP=1 -U postgres -d bifrost_dev
  python scripts/db/drop_trade_compat.py --env prod --commit | psql ... -d bifrost_prod
  python scripts/db/drop_trade_compat.py --env prod --reverse --commit | psql ... -d bifrost_prod
  python scripts/db/drop_trade_compat.py --export-sql      # the read-only COPY ... TO STDOUT of the table
  python scripts/db/drop_trade_compat.py --restore-sql     # COPY ... FROM STDIN, then setval (after --reverse)

Without --commit it ends ROLLBACK: a dry run that runs the guards and prints the report. Run it
after the env's core 0.47.0 release (its guard refuses before), and export the frozen table to
CSV before the commit. See bifrost_core.persistence.postgres.drop_trade_compat for the steps.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    from bifrost_core.persistence.postgres.drop_trade_compat import (
        ENVS,
        EXPORT_SQL,
        RESTORE_SQL,
        SEQUENCE_SQL,
        forward_sql,
        reverse_sql,
    )

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", choices=sorted(ENVS), help="the env database the SQL is for")
    p.add_argument("--commit", action="store_true", help="end with COMMIT (default: ROLLBACK, a dry run)")
    p.add_argument("--reverse", action="store_true", help="put the objects back (the table empty)")
    p.add_argument("--export-sql", action="store_true", help="print the CSV export statement")
    p.add_argument("--restore-sql", action="store_true", help="print the CSV restore statement")
    args = p.parse_args()
    if args.export_sql or args.restore_sql:
        sys.stdout.write((EXPORT_SQL if args.export_sql else RESTORE_SQL + "\n" + SEQUENCE_SQL) + "\n")
        return 0
    if not args.env:
        p.error("--env is required")
    build = reverse_sql if args.reverse else forward_sql
    sys.stdout.write(build(args.env, commit=args.commit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
