"""Nightly book snapshot (W4): ``python -m bifrost_core.portfolio.snapshot [capture|enrich|all]``.

Run by the per-env CronJob from the api image, signed in as the env's runtime role:

  python -m bifrost_core.portfolio.snapshot capture            # after the close
  python -m bifrost_core.portfolio.snapshot enrich             # once the vendor EOD is in
  python -m bifrost_core.portfolio.snapshot enrich --date 2026-10-05

The config is ``$BIFROST_CONFIG`` (default ``/app/config/runtime.yaml``), as db-init reads it;
connection settings fall back to the PG* environment (core ``connection``). ``capture`` on a
date other than today's New York session is refused: the broker tables hold today's book only.
A closed session (weekend, full-day NYSE holiday) is skipped. Exit 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date


def _load_config() -> dict:
    path = os.environ.get("BIFROST_CONFIG", "/app/config/runtime.yaml")
    if not os.path.isfile(path):
        return {}
    import yaml

    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("step", choices=("capture", "enrich", "all"), nargs="?", default="all")
    p.add_argument("--date", help="session date YYYY-MM-DD (enrich only; default: today in New York)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from bifrost_core.monitor.reader.write_support import open_conn
    from bifrost_core.persistence.postgres.connection import get_conn_params
    from bifrost_core.portfolio.snapshot.daily import (
        SnapshotError,
        capture,
        enrich,
        is_closed_session,
        session_date_ny,
    )

    config = _load_config()
    params = get_conn_params(config)
    conn = open_conn(config)  # connect timeout 10 s (TD-46)
    try:
        today = session_date_ny(conn)
        day = date.fromisoformat(args.date) if args.date else today
        if args.step in ("capture", "all") and day != today:
            print(f"capture is for today's session only ({today}); got {day}", file=sys.stderr)
            return 1
        if is_closed_session(conn, day):
            print(json.dumps({"date": day.isoformat(), "skipped": "closed session"}))
            return 0
        out: dict = {"date": day.isoformat(), "db": params.get("dbname")}
        if args.step in ("capture", "all"):
            out["capture"] = capture(conn, day)
        if args.step in ("enrich", "all"):
            out["enrich"] = enrich(conn, day)
        print(json.dumps(out))
        return 0
    except SnapshotError as e:
        print(f"snapshot failed: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"snapshot failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
