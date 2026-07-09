#!/usr/bin/env python3
"""Seed Bull Call Spread and Bear Call Spread strategy templates (idempotent)."""

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
    parser = argparse.ArgumentParser(
        description="Seed bull_call_spread and bear_call_spread strategy templates."
    )
    parser.add_argument("--config", default=None, metavar="PATH")
    args, argv_remainder = parser.parse_known_args(sys.argv[1:])

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
    from bifrost_core.persistence.postgres.seed_call_spread_templates import (
        seed_call_spread_templates,
    )

    with open(config_path, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    try:
        ids = seed_call_spread_templates(config)
    except Exception as e:
        print(f"Seed failed: {e}", file=sys.stderr)
        return 1

    for code, tid in ids.items():
        print(f"  {code} → strategy_template_id={tid}")
    print("Call spread templates ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
