"""TD-240 plan B: quote-mirror read/write functions stay deleted.

DDL and the table-name constant may still name the table. The functions may not.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
SKIP_NAMES = frozenset({"ddl.py", "brokerage_ddl.py", "brokerage_tables.py"})
PATTERN = re.compile(r"write_contract_quote_live|get_contract_quotes")


def test_src_has_no_contract_quote_read_or_write_functions() -> None:
    hits: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in SKIP_NAMES:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if PATTERN.search(line):
                rel = path.relative_to(SRC)
                hits.append(f"{rel}:{lineno}:{line.strip()}")
    assert hits == []
