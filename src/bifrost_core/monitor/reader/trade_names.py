"""Trade names beside the instance names (naming program R1, TD-26 / TD-82; core 0.42.0).

The entity the UI calls a Trade was ``strategy_instance`` in the database until R3 (core
0.45.0 renamed the tables and columns: ``trade.trade_id``, ``trade_execution.split_quantity``,
``trade_review.tags_*_json``; SQL aliases the new columns back to the old row keys). Since
core 0.42.0 every reader row that carries an instance key carries its trade name too, and
every writer that takes an instance key takes the trade name as well:

==================================  ===============================
old (kept until R4)                 new
==================================  ===============================
``strategy_instance_id``            ``trade_id``
``strategy_instance_label``         ``trade_label``
``strategy_instance_opened_at_epoch``  ``trade_opened_at_epoch``
``realized_by_strategy_instance``   ``realized_by_trade``
``instance_allocations``            ``fill_splits``
  item ``{strategy_instance_id,     item ``{trade_id, quantity,
  allocated_quantity, ...}``          strategy_opportunity_id?, trade_label?}``
``trade_review.tags_added`` / ``tags_dropped``  ``tags_added_json`` / ``tags_dropped_json``
==================================  ===============================

Readers add the new key beside the old one (same value); writers read the new name
and, when both are sent, the new one wins. ``trade_id`` here is always the Trade's
id: IB's own TradeID never leaves Golden Source (core's fill reads list their
columns and do not select it).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

# (old key, new key) on reader rows and writer fields.
ROW_KEYS: Tuple[Tuple[str, str], ...] = (
    ("strategy_instance_id", "trade_id"),
    ("strategy_instance_label", "trade_label"),
    ("strategy_instance_opened_at_epoch", "trade_opened_at_epoch"),
)

OLD_SPLITS = "instance_allocations"
NEW_SPLITS = "fill_splits"

# trade_review's jsonb columns, read under the names they take in R3 (TD-26 item 5).
REVIEW_TAG_KEYS: Tuple[Tuple[str, str], ...] = (
    ("tags_added", "tags_added_json"),
    ("tags_dropped", "tags_dropped_json"),
)


def fill_split(item: Dict[str, Any]) -> Dict[str, Any]:
    """One ``instance_allocations`` item as a fill split: ``{trade_id, quantity}``, plus the
    opportunity and the trade's label when the item has them."""
    out: Dict[str, Any] = {
        "trade_id": item.get("strategy_instance_id"),
        "quantity": item.get("allocated_quantity"),
    }
    if "strategy_opportunity_id" in item:
        out["strategy_opportunity_id"] = item.get("strategy_opportunity_id")
    label = item.get("strategy_instance_label")
    if label is not None:
        out["trade_label"] = label
    return out


def add_trade_names(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Add the trade names beside the instance names on ``row`` (in place); return it."""
    if not isinstance(row, dict):
        return row
    for old, new in ROW_KEYS:
        if old in row:
            row[new] = row[old]
    splits = row.get(OLD_SPLITS)
    if isinstance(splits, list):
        row[NEW_SPLITS] = [fill_split(a) if isinstance(a, dict) else a for a in splits]
    elif OLD_SPLITS in row:
        row[NEW_SPLITS] = splits
    return row


def add_trade_names_all(rows: Optional[Iterable[Dict[str, Any]]]) -> Any:
    """``add_trade_names`` on each row; returns ``rows`` (a list stays the same list)."""
    if rows is None:
        return rows
    for r in rows:
        add_trade_names(r)
    return rows


def add_review_tag_names(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A trade_review row with ``tags_*_json`` beside ``tags_*`` and ``trade_id``; in place."""
    if not isinstance(row, dict):
        return row
    for old, new in REVIEW_TAG_KEYS:
        if old in row:
            row[new] = row[old]
    return add_trade_names(row)


def split_item_as_instance(item: Any) -> Any:
    """A split item sent with the new names (``trade_id`` / ``quantity``) under the names the
    writers take (``strategy_instance_id`` / ``allocated_quantity``); the new name wins."""
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if "trade_id" in out:
        out["strategy_instance_id"] = out.pop("trade_id")
    if "quantity" in out:
        out["allocated_quantity"] = out.pop("quantity")
    return out


def fields_as_instance(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Writer fields with the new names mapped onto the old ones (a copy).

    ``trade_id`` -> ``strategy_instance_id``, ``fill_splits`` -> ``instance_allocations``
    (each item's ``trade_id`` / ``quantity`` too). When both names are sent the new one
    wins and the old one is dropped. Items sent under the old name are taken as they are.
    """
    if not isinstance(fields, dict):
        return fields  # type: ignore[return-value]
    out = dict(fields)
    if "trade_id" in out:
        out["strategy_instance_id"] = out.pop("trade_id")
    if NEW_SPLITS in out:
        splits = out.pop(NEW_SPLITS)
        out[OLD_SPLITS] = [split_item_as_instance(a) for a in splits] if isinstance(splits, list) else splits
    elif isinstance(out.get(OLD_SPLITS), list):
        out[OLD_SPLITS] = [split_item_as_instance(a) for a in out[OLD_SPLITS]]
    return out


def review_fields_as_columns(fields: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Review fields with ``tags_*_json`` mapped onto ``tags_*`` (a copy; the new name wins)."""
    if not isinstance(fields, dict):
        return fields  # type: ignore[return-value]
    out = dict(fields)
    for old, new in REVIEW_TAG_KEYS:
        if new in out:
            out[old] = out.pop(new)
    return out


def realized_by_trade(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """``realized_by_strategy_instance`` rows under the new name: each row a copy with ``trade_id``."""
    return [add_trade_names(dict(r)) for r in rows]  # type: ignore[misc]
