"""TD-74: ``settings.flex_default_range_days`` / ``flex_init_range_days`` leave core.

The Flex Query plugin keeps the range in Golden Source ``ops_jobs.flex_settings`` (plugin
0.7.0) and core has not read the columns since 0.39.0. The DDL no longer declares them and no
migration names them, so an Owner db-step can drop them (infra
``db-steps.d/2026-10-10-td74-drop-settings-flex-columns``) and db-init never adds them back.
The real-Postgres half is ``test_td74_settings_flex_columns_db.py``.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

from bifrost_core.monitor.reader import settings
from bifrost_core.persistence.postgres.wave13_migrations import wave13_statements

COLUMNS = ("flex_default_range_days", "flex_init_range_days")
PERSISTENCE = Path(settings.__file__).resolve().parents[2] / "persistence"


def test_the_settings_ddl_does_not_declare_them() -> None:
    ddl = (PERSISTENCE / "postgres" / "ddl.py").read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE IF NOT EXISTS settings \((.*?)\n\s*\)\s*\n", ddl, re.S)
    assert m, "no CREATE TABLE for settings"
    for column in COLUMNS:
        assert column not in m.group(1), f"settings.{column} is back in the DDL"


def _code_strings(path: Path) -> list:
    """Every string literal in a module except its docstrings (they may say where the columns went)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docs.add(id(first.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


def test_no_persistence_code_names_them() -> None:
    for path in PERSISTENCE.rglob("*.py"):
        for text in _code_strings(path):
            for column in COLUMNS:
                assert column not in text, f"{path.name} names settings.{column}"
    for stmt in wave13_statements():
        for column in COLUMNS:
            assert column not in stmt


def test_the_settings_reader_and_writers_do_not_name_them() -> None:
    for fn in (settings.get_ib_config, settings.write_ib_config, settings.write_active_strategy_and_gates):
        code = inspect.getsource(fn)
        body = code.split('"""', 2)[-1]  # past the docstring, which says where they went
        for column in COLUMNS:
            assert column not in body, fn.__name__
