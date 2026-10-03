"""Daemon gate defaults come from GateParams, not a file beside the config (debt TD-53)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bifrost_core.config import settings
from bifrost_core.monitor.schemas.gate_params import GateParams

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "config.yaml.example"


def _flat(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def test_no_file_is_needed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The daemon used to raise FileNotFoundError without config.yaml.example beside its config."""
    monkeypatch.setenv("BIFROST_CONFIG", str(tmp_path / "runtime.yaml"))
    hedge = settings.get_hedge_config({})
    assert hedge["min_hedge_shares"] == 10 and hedge["threshold_hedge_shares"] == 25
    assert settings.get_risk_config({})["paper_trade"] is True


def test_defaults_equal_the_example_they_replace() -> None:
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))["gates"]
    assert _flat(GateParams().model_dump(mode="json")) == _flat(example)


def test_runtime_config_wins() -> None:
    cfg = {"gates": {"intent": {"hedge": {"min_hedge_shares": 7}}}}
    assert settings.get_hedge_config(cfg)["min_hedge_shares"] == 7
