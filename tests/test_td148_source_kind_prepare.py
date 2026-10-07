"""TD-148: the prepared CHECK and the live allowlist stay pinned to each other.

Fresh DDL and the Owner script allow lens and backtest_run. The reader and the
request schema still allow only the five values that are in the live databases
until that DDL has been applied and core bumps. Widening one side without the
other fails this test.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from bifrost_core.monitor.reader.strategy_plan import _SOURCE_KINDS
from bifrost_core.monitor.schemas.strategy_plans import SourceKind

_ROOT = Path(__file__).resolve().parents[1]
_LIVE = {"manual", "symbol", "hypothesis", "inbox_draft", "roll"}
_PREPARED = _LIVE | {"lens", "backtest_run"}


def _in_list(text: str) -> set[str]:
    match = re.search(r"source_kind IN \(([^)]*)\)", text, re.S)
    assert match, "no source_kind IN list"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def test_fresh_ddl_and_owner_script_share_the_widened_set() -> None:
    ddl = (_ROOT / "src/bifrost_core/persistence/postgres/trade_ddl.py").read_text()
    sql = (_ROOT / "scripts/db/2026-10-07-td148-strategy-plan-source-kind.sql").read_text()
    assert _in_list(ddl) == _PREPARED
    assert _in_list(sql) == _PREPARED
    assert "strategy_plan_source_kind_check" in sql


def test_runtime_allowlist_matches_the_live_check() -> None:
    assert set(_SOURCE_KINDS) == _LIVE
    assert set(get_args(SourceKind)) == _LIVE


def test_td103_index_is_concurrent_and_partial() -> None:
    index = (_ROOT / "scripts/db/2026-10-07-td103-flex-transaction-id-index.sql").read_text()
    assert "CREATE UNIQUE INDEX CONCURRENTLY" in index
    assert "WHERE flex_transaction_id IS NOT NULL" in index
    assert not re.search(r"(?m)^BEGIN\b", index)
    dry = (_ROOT / "scripts/db/2026-10-07-td103-flex-transaction-id.sql").read_text()
    assert "would_fill" in dry
    assert "duplicate_id_groups" in dry
