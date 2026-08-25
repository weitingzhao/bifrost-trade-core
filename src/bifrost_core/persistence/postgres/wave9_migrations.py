"""Wave 9: collapse strategy child tables + gate_safety flat columns into jsonb."""

from __future__ import annotations

import json
from typing import Any

from bifrost_core.monitor.reader.gate_safety import build_gate_params_from_flat_row
from bifrost_core.monitor.reader.strategy_dim_catalog import DIM_TYPE_TO_ENUM, dim_literals_by_type
from bifrost_core.monitor.schemas.gate_params import GateParams

_WAVE9_RETIRED_TABLES = (
    "strategy_template_leg",
    "strategy_structure_leg",
    "strategy_opportunity_symbol",
    "strategy_opportunity_entry_condition",
    "gate_safety_strategy_earnings_dates",
    "strategy_dim",
)

_DIM_TYPE_TO_ENUM = DIM_TYPE_TO_ENUM

_DIM_COL_TO_TYPE = {
    "dim_direction": "direction",
    "dim_structure": "structure",
    "dim_coverage": "coverage",
    "dim_risk": "risk",
    "dim_volatility": "volatility",
    "dim_time": "time",
}

_GATE_FLAT_COLUMNS = (
    "min_dte",
    "max_dte",
    "atm_band_pct",
    "blackout_days_before",
    "blackout_days_after",
    "trading_hours_only",
    "epsilon_band",
    "threshold_hedge_shares",
    "max_delta_limit",
    "vol_window_min",
    "stale_ts_threshold_ms",
    "wide_spread_pct",
    "extreme_spread_pct",
    "data_lag_threshold_ms",
    "min_hedge_shares",
    "cooldown_seconds",
    "max_hedge_shares_per_order",
    "min_price_move_pct",
    "max_daily_hedge_count",
    "max_position_shares",
    "max_daily_loss_usd",
    "max_net_delta_shares",
    "max_spread_pct",
    "paper_trade",
)


def _table_exists(cur: Any, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    row = cur.fetchone()
    return row is not None and row[0] is not None


def _column_exists(cur: Any, table: str, column: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return cur.fetchone() is not None


def _existing_gate_flat_columns(cur: Any) -> tuple[str, ...]:
    return tuple(
        col
        for col in _GATE_FLAT_COLUMNS
        if _column_exists(cur, "gate_safety_strategy", col)
    )


def _any_gate_flat_column_exists(cur: Any) -> bool:
    return bool(_existing_gate_flat_columns(cur))


def _flat_column_defaults() -> dict[str, Any]:
    gp = GateParams().model_dump()
    strategy = gp["strategy"]
    state = gp["state"]
    intent = gp["intent"]
    guard = gp["guard"]
    return {
        "min_dte": strategy["structure"]["min_dte"],
        "max_dte": strategy["structure"]["max_dte"],
        "atm_band_pct": strategy["structure"]["atm_band_pct"],
        "blackout_days_before": strategy["earnings"]["blackout_days_before"],
        "blackout_days_after": strategy["earnings"]["blackout_days_after"],
        "trading_hours_only": strategy["trading_hours_only"],
        "epsilon_band": state["delta"]["epsilon_band"],
        "threshold_hedge_shares": state["delta"]["threshold_hedge_shares"],
        "max_delta_limit": state["delta"]["max_delta_limit"],
        "vol_window_min": state["market"]["vol_window_min"],
        "stale_ts_threshold_ms": state["market"]["stale_ts_threshold_ms"],
        "wide_spread_pct": state["liquidity"]["wide_spread_pct"],
        "extreme_spread_pct": state["liquidity"]["extreme_spread_pct"],
        "data_lag_threshold_ms": state["system"]["data_lag_threshold_ms"],
        "min_hedge_shares": intent["hedge"]["min_hedge_shares"],
        "cooldown_seconds": intent["hedge"]["cooldown_seconds"],
        "max_hedge_shares_per_order": intent["hedge"]["max_hedge_shares_per_order"],
        "min_price_move_pct": intent["hedge"]["min_price_move_pct"],
        "max_daily_hedge_count": guard["risk"]["max_daily_hedge_count"],
        "max_position_shares": guard["risk"]["max_position_shares"],
        "max_daily_loss_usd": guard["risk"]["max_daily_loss_usd"],
        "max_net_delta_shares": guard["risk"]["max_net_delta_shares"],
        "max_spread_pct": guard["risk"]["max_spread_pct"],
        "paper_trade": guard["risk"]["paper_trade"],
    }


def _params_json_is_empty(raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return True
    if isinstance(raw, dict):
        return len(raw) == 0
    return True


def _merge_flat_row_with_defaults(
    row_dict: dict[str, Any], existing_flat: tuple[str, ...]
) -> dict[str, Any]:
    merged = _flat_column_defaults()
    for col in existing_flat:
        if col in row_dict and row_dict[col] is not None:
            merged[col] = row_dict[col]
    return merged


def _migrate_template_legs(cur: Any) -> None:
    if not _table_exists(cur, "strategy_template_leg"):
        return
    cur.execute(
        """
        ALTER TABLE strategy_template
          ADD COLUMN IF NOT EXISTS legs_json jsonb NOT NULL DEFAULT '[]'::jsonb
        """
    )
    cur.execute(
        """
        UPDATE strategy_template t SET legs_json = COALESCE(agg.legs, '[]'::jsonb)
        FROM (
            SELECT strategy_template_id,
                   jsonb_agg(
                       jsonb_build_object(
                           'role', role,
                           'direction', direction,
                           'option_right', option_right,
                           'quantity', quantity_default,
                           'quantity_default', quantity_default,
                           'sort_order', sort_order
                       )
                       ORDER BY sort_order
                   ) AS legs
            FROM strategy_template_leg
            GROUP BY strategy_template_id
        ) agg
        WHERE t.strategy_template_id = agg.strategy_template_id
        """
    )


def _migrate_structure_legs(cur: Any) -> None:
    if not _table_exists(cur, "strategy_structure_leg"):
        return
    cur.execute(
        """
        ALTER TABLE strategy_structure
          ADD COLUMN IF NOT EXISTS legs_json jsonb NOT NULL DEFAULT '[]'::jsonb
        """
    )
    cur.execute(
        """
        UPDATE strategy_structure s SET legs_json = COALESCE(agg.legs, '[]'::jsonb)
        FROM (
            SELECT strategy_structure_id,
                   jsonb_agg(
                       jsonb_build_object(
                           'role', role,
                           'direction', direction,
                           'option_right', option_right,
                           'quantity', quantity,
                           'strike', strike,
                           'expiration', expiration,
                           'sort_order', sort_order
                       )
                       ORDER BY sort_order
                   ) AS legs
            FROM strategy_structure_leg
            GROUP BY strategy_structure_id
        ) agg
        WHERE s.strategy_structure_id = agg.strategy_structure_id
        """
    )


def _migrate_opportunity_json(cur: Any) -> None:
    cur.execute(
        """
        ALTER TABLE strategy_opportunity
          ADD COLUMN IF NOT EXISTS entry_conditions_json jsonb NOT NULL DEFAULT '[]'::jsonb,
          ADD COLUMN IF NOT EXISTS symbols_json jsonb NOT NULL DEFAULT '[]'::jsonb
        """
    )
    if _table_exists(cur, "strategy_opportunity_symbol"):
        cur.execute(
            """
            UPDATE strategy_opportunity o SET symbols_json = COALESCE(agg.symbols, '[]'::jsonb)
            FROM (
                SELECT strategy_opportunity_id,
                       to_jsonb(array_agg(symbol ORDER BY sort_order)) AS symbols
                FROM strategy_opportunity_symbol
                GROUP BY strategy_opportunity_id
            ) agg
            WHERE o.strategy_opportunity_id = agg.strategy_opportunity_id
            """
        )
    if _table_exists(cur, "strategy_opportunity_entry_condition"):
        cur.execute(
            """
            UPDATE strategy_opportunity o SET entry_conditions_json = COALESCE(agg.conds, '[]'::jsonb)
            FROM (
                SELECT strategy_opportunity_id,
                       jsonb_agg(
                           jsonb_build_object(
                               'condition_type', condition_type,
                               'value_text', value_text,
                               'value_numeric', value_numeric,
                               'sort_order', sort_order
                           )
                           ORDER BY sort_order
                       ) AS conds
                FROM strategy_opportunity_entry_condition
                GROUP BY strategy_opportunity_id
            ) agg
            WHERE o.strategy_opportunity_id = agg.strategy_opportunity_id
            """
        )


def _migrate_gate_params(cur: Any) -> None:
    existing_flat = _existing_gate_flat_columns(cur)
    if not existing_flat:
        return
    cur.execute(
        """
        ALTER TABLE gate_safety_strategy
          ADD COLUMN IF NOT EXISTS params_json jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )
    base_keys = [
        "gate_safety_strategy_id",
        "name",
        "version",
        "dim_direction",
        "dim_structure",
        "dim_coverage",
        "dim_risk",
        "dim_volatility",
        "dim_time",
        "is_active",
        "params_json",
    ]
    flat_sql = ", ".join(existing_flat)
    cur.execute(
        f"""
        SELECT {", ".join(base_keys)}, {flat_sql}
        FROM gate_safety_strategy
        """
    )
    rows = cur.fetchall()
    for row in rows:
        keys = [*base_keys, *existing_flat]
        row_dict = dict(zip(keys, row, strict=True))
        if not _params_json_is_empty(row_dict.get("params_json")):
            continue
        gid = int(row_dict["gate_safety_strategy_id"])
        earnings: list[str] = []
        if _table_exists(cur, "gate_safety_strategy_earnings_dates"):
            cur.execute(
                """
                SELECT holiday_date::text
                FROM gate_safety_strategy_earnings_dates
                WHERE gate_safety_strategy_id = %s ORDER BY holiday_date
                """,
                (gid,),
            )
            earnings = [str(r[0]) for r in cur.fetchall()]
        flat_row = _merge_flat_row_with_defaults(row_dict, existing_flat)
        params = build_gate_params_from_flat_row(flat_row, earnings)
        cur.execute(
            "UPDATE gate_safety_strategy SET params_json = %s::jsonb WHERE gate_safety_strategy_id = %s",
            (json.dumps(params), gid),
        )


def ensure_dim_enum_types(cur: Any) -> None:
    """Create dim_*_t enum types from strategy_dim_catalog literals (Wave 10 canonical source)."""
    literals_map = dim_literals_by_type()
    for dim_type, enum_name in DIM_TYPE_TO_ENUM.items():
        literals = literals_map.get(dim_type, ())
        if not literals:
            continue
        labels_sql = ", ".join("'" + v.replace("'", "''") + "'" for v in literals)
        cur.execute(
            f"""
            DO $enum$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{enum_name}') THEN
                CREATE TYPE {enum_name} AS ENUM ({labels_sql});
              END IF;
            END $enum$;
            """
        )


def _collect_dim_literals(cur: Any, dim_type: str) -> list[str]:
    literals: set[str] = set()
    if _table_exists(cur, "strategy_dim"):
        cur.execute(
            "SELECT DISTINCT code FROM strategy_dim WHERE dim_type = %s AND code IS NOT NULL",
            (dim_type,),
        )
        for (code,) in cur.fetchall():
            if code:
                literals.add(str(code).strip())
    col = f"dim_{dim_type}"
    for table in ("strategy_template", "gate_safety_strategy"):
        if not _table_exists(cur, table):
            continue
        if not _column_exists(cur, table, col):
            continue
        cur.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL")
        for (val,) in cur.fetchall():
            if val:
                literals.add(str(val).strip())
    return sorted(literals)


def _create_dim_enums(cur: Any) -> None:
    """Legacy alias — Wave 10 uses catalog literals only."""
    ensure_dim_enum_types(cur)


def _alter_dim_columns_to_enum(cur: Any) -> None:
    for table in ("strategy_template", "gate_safety_strategy"):
        if not _table_exists(cur, table):
            continue
        for col, dim_type in _DIM_COL_TO_TYPE.items():
            enum_name = _DIM_TYPE_TO_ENUM[dim_type]
            if not _column_exists(cur, table, col):
                continue
            cur.execute(
                f"""
                DO $alter$
                BEGIN
                  IF EXISTS (SELECT 1 FROM pg_type WHERE typname = '{enum_name}')
                     AND EXISTS (
                       SELECT 1 FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = '{table}'
                         AND column_name = '{col}' AND udt_name <> '{enum_name}'
                     ) THEN
                    ALTER TABLE {table}
                      ALTER COLUMN {col} TYPE {enum_name}
                      USING ({col}::{enum_name});
                  END IF;
                END $alter$;
                """
            )


def _drop_gate_flat_columns(cur: Any) -> None:
    for col in _GATE_FLAT_COLUMNS:
        cur.execute(f"ALTER TABLE gate_safety_strategy DROP COLUMN IF EXISTS {col}")


def _drop_retired_tables(cur: Any) -> None:
    for name in _WAVE9_RETIRED_TABLES:
        cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")


def migrate_wave9_strategy_collapse(cur: Any) -> None:
    """Idempotent Wave 9 migration: jsonb collapse + enum dims + drop child tables."""
    if not _table_exists(cur, "strategy_template"):
        return
    _migrate_template_legs(cur)
    _migrate_structure_legs(cur)
    _migrate_opportunity_json(cur)
    _migrate_gate_params(cur)
    _create_dim_enums(cur)
    _alter_dim_columns_to_enum(cur)
    _drop_retired_tables(cur)
    _drop_gate_flat_columns(cur)
