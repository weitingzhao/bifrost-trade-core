"""TD-80 C2-b (core 0.48.0): ``StatusReader`` is a read-only facade, and stays one.

Its last five write methods (``create_trade`` and its R4 alias ``create_strategy_instance``,
``create_position_category``, ``set_position_category_tag``, ``set_instrument_class``,
``set_market_streams_symbol_order``) and the bool / ``(id, error)`` module writers behind them
left in this release; the API writes through the ``*_strict`` / ``patch_*`` writers since api 0.9.0.

Two checks, because a name alone proves little:

1. **Names.** No public member is named like a write (``create_`` / ``set_`` / ``update_`` / ...).
2. **What it calls.** Every function a public method delegates to (one level: the module
   function it calls) has no write in its source -- no ``INSERT INTO`` / ``UPDATE .. SET`` /
   ``DELETE FROM`` / ``TRUNCATE`` / ``UPSERT``, no ``commit()``, no ``write_connection`` /
   ``write_transaction``, no Redis write verb. The facade's own ``_connect`` commits its
   ``SET`` session limits; that private helper is the one exception and is checked to do
   nothing else.

A positive control proves check 2 sees a real writer.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import textwrap
from typing import Callable, Dict, List

from bifrost_core.monitor.reader import common
from bifrost_core.monitor.reader.common import StatusReader
from bifrost_core.portfolio.reader import position_categories

WRITE_PREFIXES = (
    "create_", "set_", "update_", "delete_", "add_", "remove_", "write_", "insert_",
    "upsert_", "batch_", "save_", "patch_", "put_", "replace_", "link_", "unlink_",
    "clear_", "mark_", "record_", "store_", "drop_", "reset_",
)

_WRITE_SOURCE = re.compile(
    r"\bINSERT\s+INTO\b|\bUPDATE\s+[\w.]+\s+SET\b|\bDELETE\s+FROM\b|\bTRUNCATE\b|\bUPSERT\b"
    r"|\.commit\(|\bwrite_connection\b|\bwrite_transaction\b"
    r"|\.(?:hset|hdel|xadd|lpush|rpush|publish|setex|expire|incr|sadd|srem|zadd)\("
)


def _public_members() -> List[str]:
    return sorted(n for n in dir(StatusReader) if not n.startswith("_"))


def _method_node(name: str) -> ast.FunctionDef:
    src = textwrap.dedent(inspect.getsource(getattr(StatusReader, name)))
    node = ast.parse(src).body[0]
    assert isinstance(node, ast.FunctionDef), name
    return node


def _delegates(node: ast.FunctionDef) -> Dict[str, Callable]:
    """Module functions a method calls: ``<module alias>.<function>(...)`` for the modules ``common``
    imports, and whatever a ``from .. import ..`` inside the method brings in (a module or a function)."""
    local = {}
    for n in ast.walk(node):
        if isinstance(n, ast.ImportFrom) and n.module:
            parent = importlib.import_module(n.module)
            for a in n.names:
                local[a.asname or a.name] = getattr(parent, a.name, None) or importlib.import_module(
                    f"{n.module}.{a.name}"
                )
    out: Dict[str, Callable] = {}
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Name) and inspect.isfunction(local.get(n.func.id)):
            fn = local[n.func.id]
            out[f"{fn.__module__}.{fn.__name__}"] = fn
        elif isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name):
            owner = local.get(n.func.value.id) or getattr(common, n.func.value.id, None)
            if inspect.ismodule(owner):
                out[f"{owner.__name__}.{n.func.attr}"] = getattr(owner, n.func.attr)
    return out


def _writes_in(fn: Callable) -> List[str]:
    return sorted({m.group(0) for m in _WRITE_SOURCE.finditer(inspect.getsource(fn))})


def test_no_public_member_is_named_like_a_write() -> None:
    named = [n for n in _public_members() if n.startswith(WRITE_PREFIXES)]
    assert named == [], f"StatusReader is read-only (TD-80 C2-b); write through the *_strict writers: {named}"


def test_the_c2b_writers_are_gone_from_the_facade_and_the_modules() -> None:
    from bifrost_core.monitor.reader import strategy_instance
    from bifrost_core.portfolio.reader import instrument_class

    for name in ("create_trade", "create_strategy_instance", "create_position_category",
                 "set_position_category_tag", "set_instrument_class", "set_market_streams_symbol_order"):
        assert not hasattr(StatusReader, name), name
    gone = {
        strategy_instance: ("create_instance",),
        position_categories: (
            "create_position_category", "set_position_category_tag", "set_market_streams_symbol_order",
        ),
        instrument_class: ("set_instrument_class",),
    }
    for module, names in gone.items():
        for name in names:
            assert not hasattr(module, name), f"{module.__name__}.{name}"
            assert hasattr(module, f"{name}_strict"), f"{module.__name__}.{name}_strict"


def test_no_public_method_delegates_to_a_writer() -> None:
    checked = 0
    found: Dict[str, List[str]] = {}
    for name in _public_members():
        member = inspect.getattr_static(StatusReader, name)
        if isinstance(member, property):
            assert member.fset is None and member.fdel is None, name
            continue
        node = _method_node(name)
        own = _writes_in(getattr(StatusReader, name))
        if own:
            found[name] = own
        for target, fn in _delegates(node).items():
            checked += 1
            hits = _writes_in(fn)
            if hits:
                found[f"{name} -> {target}"] = hits
    assert found == {}, found
    assert checked >= 50  # the scan really walked the delegates (about 60 today)


def test_the_check_sees_a_real_writer() -> None:
    assert "write_connection" in " ".join(_writes_in(position_categories.set_market_streams_symbol_order_strict))
    assert any("INSERT" in h for h in _writes_in(position_categories.create_position_category_strict))


def test_connect_commits_only_its_session_limits() -> None:
    src = inspect.getsource(StatusReader._connect)
    assert src.count(".commit(") == 1
    executed = re.findall(r'cur\.execute\("([^"]+)"\)', src)
    assert executed and all(s.startswith("SET ") for s in executed), executed
    assert not _WRITE_SOURCE.search(src.replace("self._conn.commit()", ""))
