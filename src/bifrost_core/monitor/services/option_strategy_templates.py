"""Type Config / option template UI option payloads (mirror GET /strategies/templates/options/*)."""

from __future__ import annotations

from typing import Any, Dict

from bifrost_core.monitor.reader import structure_type_config_constants


def param_kind_options_payload() -> Dict[str, Any]:
    return {"options": structure_type_config_constants.get_param_kind_options_with_labels()}


def leg_role_options_payload() -> Dict[str, Any]:
    return {"options": structure_type_config_constants.get_leg_role_options_with_labels()}


def leg_direction_options_payload() -> Dict[str, Any]:
    return {"options": structure_type_config_constants.get_leg_direction_options_with_labels()}


def leg_option_right_options_payload() -> Dict[str, Any]:
    return {"options": structure_type_config_constants.get_leg_option_right_options_with_labels()}


def meta_key_options_payload(structure_type: str = "covered_call") -> Dict[str, Any]:
    return {
        "options": structure_type_config_constants.get_meta_key_options_with_labels(structure_type)
    }


def meta_value_options_payload(structure_type: str, meta_key: str) -> Dict[str, Any]:
    return {
        "options": structure_type_config_constants.get_meta_value_options_with_labels(
            structure_type, meta_key
        )
    }
