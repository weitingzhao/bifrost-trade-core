"""Write structure type config: strategy_structure_type, legs, subtypes, characteristics, meta_params, infer_rules.

Used by POST/PUT/DELETE structure-types API. Uses same config/conn pattern as gate_safety_write.
"""

import logging
from typing import Any, Dict, List, Optional

import psycopg2

from bifrost_core.persistence.postgres.connection import _get_conn_params

from bifrost_core.monitor.reader import structure_type_config_constants as _const

logger = logging.getLogger(__name__)


def _conn_from_config(status_config: Optional[dict]) -> Any:
    """Open a connection from status_config (postgres). Returns None if config invalid."""
    if not status_config or (
        status_config.get("sink") != "postgres" and not status_config.get("postgres")
    ):
        return None
    try:
        params = _get_conn_params(status_config)
        return psycopg2.connect(**params)
    except Exception as e:
        logger.warning("structure_type_config_write connect failed: %s", e)
        return None


def _check_type_referenced(cur: Any, structure_type: str) -> Optional[str]:
    """Return error message if type is referenced by strategy_structure or gate_safety_strategy."""
    cur.execute(
        "SELECT 1 FROM strategy_structure WHERE structure_type = %s LIMIT 1",
        (structure_type,),
    )
    if cur.fetchone():
        return "Structure type is referenced by one or more strategy structures"
    cur.execute(
        "SELECT 1 FROM gate_safety_strategy WHERE structure_type = %s LIMIT 1",
        (structure_type,),
    )
    if cur.fetchone():
        return "Structure type is referenced by one or more gate safety sets"
    return None


def create_structure_type(status_config: Optional[dict], payload: Dict[str, Any]) -> None:
    """Insert one row into strategy_structure_type. Raises ValueError on conflict or invalid payload."""
    structure_type = (payload.get("structure_type") or "").strip()
    if not structure_type:
        raise ValueError("structure_type is required")
    display_label = (payload.get("display_label") or "").strip() or structure_type
    sort_order = int(payload["sort_order"]) if payload.get("sort_order") is not None else 0
    has_subtypes = bool(payload.get("has_subtypes", False))
    type_explanation = (payload.get("type_explanation") or "").strip() or None

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_structure_type
                    (structure_type, display_label, sort_order, has_subtypes, type_explanation)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (structure_type, display_label, sort_order, has_subtypes, type_explanation),
            )
        conn.commit()
    except psycopg2.IntegrityError as e:
        conn.rollback()
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise ValueError(f"Structure type '{structure_type}' already exists") from e
        raise ValueError(str(e)) from e
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def update_structure_type(
    status_config: Optional[dict], structure_type: str, payload: Dict[str, Any]
) -> bool:
    """Update strategy_structure_type row. Returns True if updated, False if not found."""
    structure_type = (structure_type or "").strip()
    if not structure_type:
        raise ValueError("structure_type is required")
    display_label = (payload.get("display_label") or "").strip()
    sort_order = payload.get("sort_order")
    has_subtypes = payload.get("has_subtypes")
    type_explanation = payload.get("type_explanation")
    if type_explanation is not None:
        type_explanation = (type_explanation or "").strip() or None

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            updates = []
            args: List[Any] = []
            if display_label is not None:
                updates.append("display_label = %s")
                args.append(display_label.strip() or structure_type)
            if sort_order is not None:
                updates.append("sort_order = %s")
                args.append(int(sort_order))
            if has_subtypes is not None:
                updates.append("has_subtypes = %s")
                args.append(bool(has_subtypes))
            if type_explanation is not None:
                updates.append("type_explanation = %s")
                args.append(type_explanation)
            if not updates:
                conn.close()
                return True
            updates.append("updated_at = now()")
            args.append(structure_type)
            cur.execute(
                f"UPDATE strategy_structure_type SET {', '.join(updates)} WHERE structure_type = %s",
                args,
            )
            n = cur.rowcount
        conn.commit()
        return n > 0
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def delete_structure_type(status_config: Optional[dict], structure_type: str) -> bool:
    """Delete strategy_structure_type and cascade children. Returns False if referenced or not found."""
    structure_type = (structure_type or "").strip()
    if not structure_type:
        raise ValueError("structure_type is required")

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            err = _check_type_referenced(cur, structure_type)
            if err:
                conn.rollback()
                raise ValueError(err)
            cur.execute("DELETE FROM strategy_structure_type WHERE structure_type = %s", (structure_type,))
            n = cur.rowcount
        conn.commit()
        return n > 0
    except ValueError:
        raise
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def replace_structure_type_legs(
    status_config: Optional[dict], structure_type: str, legs: List[Dict[str, Any]]
) -> None:
    """Replace all default legs for the given structure type. legs: list of role, direction, option_right, quantity_default (optional), sort_order (optional).
    role, direction, option_right are validated against allowlists in structure_type_config_constants."""
    structure_type = (structure_type or "").strip()
    if not structure_type:
        raise ValueError("structure_type is required")

    allowed_roles = set(_const.LEG_ROLE_ALLOWED)
    allowed_directions = set(_const.LEG_DIRECTION_ALLOWED)
    allowed_option_rights = {"", "C", "P"}

    for i, leg in enumerate(legs or []):
        if not isinstance(leg, dict):
            continue
        role_raw = (leg.get("role") or "").strip()
        direction_raw = (leg.get("direction") or "").strip()
        option_right_raw = (leg.get("option_right") or "").strip()
        if role_raw and role_raw not in allowed_roles:
            raise ValueError(f"leg {i}: role must be one of {sorted(allowed_roles)}, got {role_raw!r}")
        if direction_raw and direction_raw not in allowed_directions:
            raise ValueError(f"leg {i}: direction must be one of {sorted(allowed_directions)}, got {direction_raw!r}")
        if option_right_raw and option_right_raw not in allowed_option_rights:
            raise ValueError(f"leg {i}: option_right must be empty, C, or P, got {option_right_raw!r}")

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM strategy_structure_type_leg WHERE structure_type = %s", (structure_type,))
            for i, leg in enumerate(legs or []):
                if not isinstance(leg, dict):
                    continue
                sort_order = int(leg["sort_order"]) if leg.get("sort_order") is not None else i
                role = (leg.get("role") or "").strip() or None
                direction = (leg.get("direction") or "").strip() or None
                option_right = (leg.get("option_right") or "").strip() or None
                qty = int(leg["quantity_default"]) if leg.get("quantity_default") is not None else int(leg.get("quantity", 1))
                cur.execute(
                    """
                    INSERT INTO strategy_structure_type_leg
                        (structure_type, sort_order, role, direction, option_right, quantity_default)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (structure_type, sort_order, role, direction, option_right, qty),
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def replace_structure_subtype_legs(
    status_config: Optional[dict], structure_type: str, subtype: str, legs: List[Dict[str, Any]]
) -> None:
    """Replace default legs for a given (structure_type, subtype).

    legs: list of role, direction, option_right, quantity_default (optional), sort_order (optional).
    If legs is empty, all subtype legs for this (type, subtype) are deleted (subtype will inherit type-level legs).
    """
    structure_type = (structure_type or "").strip()
    subtype = (subtype or "").strip()
    if not structure_type:
        raise ValueError("structure_type is required")
    if not subtype:
        raise ValueError("subtype is required")

    allowed_roles = set(_const.LEG_ROLE_ALLOWED)
    allowed_directions = set(_const.LEG_DIRECTION_ALLOWED)
    allowed_option_rights = {"", "C", "P"}

    for i, leg in enumerate(legs or []):
        if not isinstance(leg, dict):
            continue
        role_raw = (leg.get("role") or "").strip()
        direction_raw = (leg.get("direction") or "").strip()
        option_right_raw = (leg.get("option_right") or "").strip()
        if role_raw and role_raw not in allowed_roles:
            raise ValueError(f"subtype leg {i}: role must be one of {sorted(allowed_roles)}, got {role_raw!r}")
        if direction_raw and direction_raw not in allowed_directions:
            raise ValueError(
                f"subtype leg {i}: direction must be one of {sorted(allowed_directions)}, got {direction_raw!r}"
            )
        if option_right_raw and option_right_raw not in allowed_option_rights:
            raise ValueError(
                f"subtype leg {i}: option_right must be empty, C, or P, got {option_right_raw!r}"
            )

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            # Delete existing legs for this subtype; if legs is empty, this is effectively "inherit type legs".
            cur.execute(
                "DELETE FROM strategy_structure_subtype_leg WHERE structure_type = %s AND subtype = %s",
                (structure_type, subtype),
            )
            for i, leg in enumerate(legs or []):
                if not isinstance(leg, dict):
                    continue
                sort_order = int(leg["sort_order"]) if leg.get("sort_order") is not None else i
                role = (leg.get("role") or "").strip() or None
                direction = (leg.get("direction") or "").strip() or None
                option_right = (leg.get("option_right") or "").strip() or None
                qty = (
                    int(leg["quantity_default"])
                    if leg.get("quantity_default") is not None
                    else int(leg.get("quantity", 1))
                )
                cur.execute(
                    """
                    INSERT INTO strategy_structure_subtype_leg (
                        structure_type, subtype, sort_order, role, direction, option_right, quantity_default
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (structure_type, subtype, sort_order, role, direction, option_right, qty),
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def create_subtype(
    status_config: Optional[dict], structure_type: str, payload: Dict[str, Any]
) -> None:
    """Insert one row into strategy_structure_subtype."""
    structure_type = (structure_type or "").strip()
    if not structure_type:
        raise ValueError("structure_type is required")
    subtype = (payload.get("subtype") or "").strip()
    if not subtype:
        raise ValueError("subtype is required")
    display_label = (payload.get("display_label") or "").strip() or subtype
    example = (payload.get("example") or "").strip() or None
    typical_use = (payload.get("typical_use") or "").strip() or None
    subtype_explanation = (payload.get("subtype_explanation") or "").strip() or None
    nature = (payload.get("nature") or "").strip() or None
    sort_order = int(payload["sort_order"]) if payload.get("sort_order") is not None else 0

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO strategy_structure_subtype
                    (structure_type, subtype, display_label, example, typical_use,
                     subtype_explanation, nature, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    structure_type,
                    subtype,
                    display_label,
                    example,
                    typical_use,
                    subtype_explanation,
                    nature,
                    sort_order,
                ),
            )
        conn.commit()
    except psycopg2.IntegrityError as e:
        conn.rollback()
        if "foreign key" in str(e).lower():
            raise ValueError(f"Structure type '{structure_type}' does not exist") from e
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise ValueError(f"Subtype '{subtype}' already exists for this type") from e
        raise ValueError(str(e)) from e
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def update_subtype(
    status_config: Optional[dict],
    structure_type: str,
    subtype: str,
    payload: Dict[str, Any],
) -> bool:
    """Update strategy_structure_subtype. subtype in path is the key; payload can update display_label, example, etc."""
    structure_type = (structure_type or "").strip()
    subtype = (subtype or "").strip()
    if not structure_type or not subtype:
        raise ValueError("structure_type and subtype are required")

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")

    display_label = payload.get("display_label")
    example = payload.get("example")
    typical_use = payload.get("typical_use")
    subtype_explanation = payload.get("subtype_explanation")
    nature = payload.get("nature")
    sort_order = payload.get("sort_order")
    if display_label is not None:
        display_label = (display_label or "").strip()
    if example is not None:
        example = (example or "").strip() or None
    if typical_use is not None:
        typical_use = (typical_use or "").strip() or None
    if subtype_explanation is not None:
        subtype_explanation = (subtype_explanation or "").strip() or None
    if nature is not None:
        nature = (nature or "").strip() or None

    try:
        with conn.cursor() as cur:
            updates = []
            args: List[Any] = []
            if display_label is not None:
                updates.append("display_label = %s")
                args.append((display_label or "").strip() or subtype)
            if example is not None:
                updates.append("example = %s")
                args.append(example)
            if typical_use is not None:
                updates.append("typical_use = %s")
                args.append(typical_use)
            if subtype_explanation is not None:
                updates.append("subtype_explanation = %s")
                args.append(subtype_explanation)
            if nature is not None:
                updates.append("nature = %s")
                args.append(nature)
            if sort_order is not None:
                updates.append("sort_order = %s")
                args.append(int(sort_order))
            if not updates:
                conn.close()
                return True
            updates.append("updated_at = now()")
            args.extend([structure_type, subtype])
            cur.execute(
                f"UPDATE strategy_structure_subtype SET {', '.join(updates)} WHERE structure_type = %s AND subtype = %s",
                args,
            )
            n = cur.rowcount
        conn.commit()
        return n > 0
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def delete_subtype(
    status_config: Optional[dict], structure_type: str, subtype: str
) -> bool:
    """Delete one subtype and cascade characteristics, meta_params, rules."""
    structure_type = (structure_type or "").strip()
    subtype = (subtype or "").strip()
    if not structure_type or not subtype:
        raise ValueError("structure_type and subtype are required")

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM strategy_structure_subtype WHERE structure_type = %s AND subtype = %s",
                (structure_type, subtype),
            )
            n = cur.rowcount
        conn.commit()
        return n > 0
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def replace_subtype_characteristics(
    status_config: Optional[dict],
    structure_type: str,
    subtype: str,
    items: List[str],
) -> None:
    """Replace all characteristics for the subtype. items: list of characteristic_text."""
    structure_type = (structure_type or "").strip()
    subtype = (subtype or "").strip()
    if not structure_type or not subtype:
        raise ValueError("structure_type and subtype are required")

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM strategy_structure_subtype_characteristic WHERE structure_type = %s AND subtype = %s",
                (structure_type, subtype),
            )
            for i, text in enumerate(items or []):
                t = (str(text).strip() if text is not None else "").strip()
                if not t:
                    continue
                cur.execute(
                    """
                    INSERT INTO strategy_structure_subtype_characteristic
                        (structure_type, subtype, sort_order, characteristic_text)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (structure_type, subtype, i, t),
                )
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def replace_subtype_meta_params(
    status_config: Optional[dict],
    structure_type: str,
    subtype: str,
    items: List[Dict[str, Any]],
) -> None:
    """Replace all meta params for the subtype. Each item: meta_key, display_label?, default_value_text?, param_kind?, sort_order?."""
    structure_type = (structure_type or "").strip()
    subtype = (subtype or "").strip()
    if not structure_type or not subtype:
        raise ValueError("structure_type and subtype are required")

    allowed_meta_keys = _const.get_meta_key_options(structure_type)
    param_kind_allowed = _const.get_param_kind_options()

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM strategy_structure_subtype_meta_param WHERE structure_type = %s AND subtype = %s",
                (structure_type, subtype),
            )
            for i, row in enumerate(items or []):
                if not isinstance(row, dict):
                    continue
                meta_key = (row.get("meta_key") or "").strip()
                if not meta_key:
                    continue
                if meta_key not in allowed_meta_keys:
                    raise ValueError(
                        f"meta_key '{meta_key}' is not allowed for structure_type '{structure_type}'. "
                        f"Allowed: {list(allowed_meta_keys)}"
                    )
                display_label = (row.get("display_label") or "").strip() or None
                default_value_text = (row.get("default_value_text") or "").strip() or None
                param_kind = (row.get("param_kind") or "").strip() or None
                if param_kind is not None and param_kind not in param_kind_allowed:
                    raise ValueError(
                        f"param_kind must be one of: {list(param_kind_allowed)}, got '{param_kind}'"
                    )
                allowed_values = _const.get_meta_value_options(structure_type, meta_key)
                if allowed_values and param_kind == "fixed" and default_value_text is not None:
                    if default_value_text not in allowed_values:
                        raise ValueError(
                            f"default_value_text '{default_value_text}' for meta_key '{meta_key}' "
                            f"must be one of: {list(allowed_values)}"
                        )
                sort_order = int(row["sort_order"]) if row.get("sort_order") is not None else i
                cur.execute(
                    """
                    INSERT INTO strategy_structure_subtype_meta_param
                        (structure_type, subtype, meta_key, display_label, default_value_text, param_kind, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        structure_type,
                        subtype,
                        meta_key,
                        display_label,
                        default_value_text,
                        param_kind,
                        sort_order,
                    ),
                )
        conn.commit()
    except psycopg2.IntegrityError as e:
        conn.rollback()
        if "unique" in str(e).lower():
            raise ValueError("Duplicate meta_key for this subtype") from e
        raise ValueError(str(e)) from e
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()


def replace_subtype_infer_rules(
    status_config: Optional[dict],
    structure_type: str,
    rules: List[Dict[str, Any]],
) -> None:
    """Replace all infer rules for the structure type. Each rule: meta_key, meta_value_text, subtype. Subtype must exist."""
    structure_type = (structure_type or "").strip()
    if not structure_type:
        raise ValueError("structure_type is required")

    allowed_meta_keys = _const.get_meta_key_options(structure_type)

    conn = _conn_from_config(status_config)
    if conn is None:
        raise ValueError("Database not configured")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM strategy_structure_subtype_rule WHERE structure_type = %s",
                (structure_type,),
            )
            subtype_meta_pairs: set[tuple[str, str]] = set()
            for row in rules or []:
                if not isinstance(row, dict):
                    continue
                meta_key = (row.get("meta_key") or "").strip()
                meta_value_text = (row.get("meta_value_text") or "").strip()
                subtype = (row.get("subtype") or "").strip()
                if not meta_key or not meta_value_text or not subtype:
                    continue
                if meta_key not in allowed_meta_keys:
                    raise ValueError(
                        f"meta_key '{meta_key}' is not allowed for structure_type '{structure_type}'. "
                        f"Allowed: {list(allowed_meta_keys)}"
                    )
                allowed_values = _const.get_meta_value_options(structure_type, meta_key)
                if allowed_values and meta_value_text not in allowed_values:
                    raise ValueError(
                        f"meta_value_text '{meta_value_text}' for meta_key '{meta_key}' "
                        f"must be one of: {list(allowed_values)}"
                    )
                cur.execute(
                    """
                    INSERT INTO strategy_structure_subtype_rule
                        (structure_type, subtype, meta_key, meta_value_text)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (structure_type, subtype, meta_key, meta_value_text),
                )
                subtype_meta_pairs.add((subtype, meta_key))

            # Keep subtype-level fixed meta_params in sync with infer rules:
            # - For every (subtype, meta_key) in rules, ensure there is a meta_param row with param_kind='fixed'
            #   and default_value_text = meta_value_text.
            # - Remove fixed meta_params for this structure_type that no longer correspond to any rule.
            if subtype_meta_pairs:
                # Delete obsolete fixed meta_params for this structure_type that are not referenced by any rule
                cur.execute(
                    """
                    DELETE FROM strategy_structure_subtype_meta_param
                    WHERE structure_type = %s
                      AND param_kind = 'fixed'
                      AND (subtype, meta_key) NOT IN (
                        SELECT DISTINCT subtype, meta_key
                        FROM strategy_structure_subtype_rule
                        WHERE structure_type = %s
                      )
                    """,
                    (structure_type, structure_type),
                )

                # Upsert fixed meta_params from current rules
                for row in rules or []:
                    if not isinstance(row, dict):
                        continue
                    meta_key = (row.get("meta_key") or "").strip()
                    meta_value_text = (row.get("meta_value_text") or "").strip()
                    subtype = (row.get("subtype") or "").strip()
                    if not meta_key or not meta_value_text or not subtype:
                        continue
                    cur.execute(
                        """
                        INSERT INTO strategy_structure_subtype_meta_param
                            (structure_type, subtype, meta_key, display_label, default_value_text, param_kind, sort_order)
                        VALUES (%s, %s, %s, NULL, %s, 'fixed', 0)
                        ON CONFLICT (structure_type, subtype, meta_key) DO UPDATE SET
                            default_value_text = EXCLUDED.default_value_text,
                            param_kind = EXCLUDED.param_kind
                        """,
                        (structure_type, subtype, meta_key, meta_value_text),
                    )
        conn.commit()
    except psycopg2.IntegrityError as e:
        conn.rollback()
        if "foreign key" in str(e).lower():
            raise ValueError("One or more subtypes in infer rules do not exist for this type") from e
        if "unique" in str(e).lower():
            raise ValueError("Duplicate (meta_key, meta_value_text) in infer rules") from e
        raise ValueError(str(e)) from e
    except Exception as e:
        conn.rollback()
        raise ValueError(str(e)) from e
    finally:
        conn.close()
