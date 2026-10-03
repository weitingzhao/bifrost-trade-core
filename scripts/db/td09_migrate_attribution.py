#!/usr/bin/env python3
"""TD-09: print the one-transaction attribution migration for one env database.

Prints SQL only (no connection, no credentials); pipe it into psql on the env database:

  python scripts/db/td09_migrate_attribution.py            | psql -X -v ON_ERROR_STOP=1 -d bifrost_dev
  python scripts/db/td09_migrate_attribution.py --views --commit | psql ... -d bifrost_prod

Without --commit it ends ROLLBACK: a dry run that prints what would be loaded and what
would be left behind. --views also rebuilds the env execution views in the same
transaction (what db-init's FDW step does), so readers switch to the new table at commit.
See bifrost_core.persistence.postgres.td09_attribution for the rules.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    from bifrost_core.persistence.postgres.td09_attribution import migration_sql

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--commit", action="store_true", help="end with COMMIT (default: ROLLBACK, a dry run)")
    p.add_argument("--views", action="store_true", help="rebuild the env execution views in the same transaction")
    p.add_argument("--schema", default="brokerage", help="schema holding the FDW raw tables (default: brokerage)")
    p.add_argument("--role", default="bifrost", help="SET LOCAL ROLE to this app role ('' to skip; default: bifrost)")
    args = p.parse_args()
    sys.stdout.write(
        migration_sql(commit=args.commit, views=args.views, schema=args.schema, role=args.role or None)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
