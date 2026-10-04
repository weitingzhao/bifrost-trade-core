#!/usr/bin/env python3
"""Naming R3: print the one-transaction rename of the Trade entity for one env database.

Prints SQL only (no connection, no credentials); pipe it into psql on that env's database,
connected as postgres (the SQL switches to the app role itself, and on DEV back to postgres for
the brokerage views, which postgres owns there):

  python scripts/db/rename_trade_entity.py --env dev | psql -X -v ON_ERROR_STOP=1 -U postgres -d bifrost_dev
  python scripts/db/rename_trade_entity.py --env prod --commit | psql ... -d bifrost_prod
  python scripts/db/rename_trade_entity.py --env prod --reverse --commit | psql ... -d bifrost_prod

Without --commit it ends ROLLBACK: a dry run that prints the before / after counts. The SQL
checks that it runs on bifrost_<env> and that the brokerage views have the owner it expects.
--reverse prints the way back (run it before rolling the images back to core 0.44.0).
See bifrost_core.persistence.postgres.rename_trade_entity for the steps.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    from bifrost_core.persistence.postgres.rename_trade_entity import ENVS, forward_sql
    from bifrost_core.persistence.postgres.rename_trade_entity_reverse import reverse_sql

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", required=True, choices=sorted(ENVS), help="the env database the SQL is for")
    p.add_argument("--commit", action="store_true", help="end with COMMIT (default: ROLLBACK, a dry run)")
    p.add_argument("--reverse", action="store_true", help="undo the rename (back to the core 0.44.0 names)")
    args = p.parse_args()
    build = reverse_sql if args.reverse else forward_sql
    sys.stdout.write(build(args.env, commit=args.commit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
