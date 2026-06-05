#!/usr/bin/env python3
"""Copy engine tests into bifrost-trade-core/tests with import rewrites."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2] / "bifrost-trader-engine" / "tests"
DEST = Path(__file__).resolve().parents[1] / "tests"

TESTS = [
    "test_config.py",
    "test_sse_queue_utils.py",
    "test_redis_quotes_ingester.py",
    "test_stock_reference.py",
    "test_stock_ohlc_massive.py",
    "test_portfolio.py",
    "test_portfolio_model.py",
    "test_option_stock_link.py",
    "test_position_attribution.py",
    "test_accounts_stk_live_stale.py",
    "test_opt_pair_calendar.py",
    "test_strategy_win_rate_aggregate.py",
    "test_symbol_normalize.py",
    "test_reference_indices_merge.py",
    "test_self_check_derive.py",
    "test_ib_probe_derived.py",
    "test_read_config_merge.py",
    "test_startup_config_path.py",
    "test_daemon_console_stream_keys.py",
]

REPLS = [
    (r"\bfrom src\.config\b", "from bifrost_core.config"),
    (r"\bfrom src\.core\b", "from bifrost_core.core"),
    (r"\bfrom src\.persistence\b", "from bifrost_core.persistence"),
    (r"\bfrom src\.portfolio\b", "from bifrost_core.portfolio"),
    (r"\bfrom src\.monitor\b", "from bifrost_core.monitor"),
    (r"\bfrom src\.app\.config\b", "from bifrost_core.config.startup"),
    (r"\bfrom src\.monitor\.reader\.executions\b", "from bifrost_core.portfolio.reader.executions"),
    (r"\bimport src\.monitor\.reader\.executions\b", "import bifrost_core.portfolio.reader.executions"),
]


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    for name in TESTS:
        src = ENGINE / name
        if not src.is_file():
            print("skip", name)
            continue
        text = src.read_text(encoding="utf-8")
        for pat, rep in REPLS:
            text = re.sub(pat, rep, text)
        (DEST / name).write_text(text, encoding="utf-8")
        print("ok", name)


if __name__ == "__main__":
    main()
