"""read_config deep-merge of config.yaml with config.dev.yaml / config.prod.yaml."""

import pytest
from pathlib import Path

from bifrost_core.config.startup import normalize_server_config, read_config

_FULL_SERVER_YAML = """
  architecture:
    monitor_port: 8000
    docs_port: 8767
    ops_port: 8768
  account:
    trading_port: 8769
    portfolio_port: 8771
  research:
    research_port: 8773
    market_port: 8772
    strategy_port: 8770
  feed:
    massive_port: 8766
"""


def test_merge_prod_overlay_wins_scalar(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "postgres:\n  database: shared\n  host: h1\n",
        encoding="utf-8",
    )
    (cfg / "config.prod.yaml").write_text(
        "postgres:\n  database: prod_db\n",
        encoding="utf-8",
    )
    merged, path = read_config(str(cfg / "config.prod.yaml"))
    assert path == str((cfg / "config.prod.yaml").resolve())
    assert merged["postgres"]["database"] == "prod_db"
    assert merged["postgres"]["host"] == "h1"


def test_merge_nested_dict(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "server:\n" + _FULL_SERVER_YAML + "  extra: x\n",
        encoding="utf-8",
    )
    (cfg / "config.dev.yaml").write_text(
        "server:\n  architecture:\n    monitor_port: 9000\n",
        encoding="utf-8",
    )
    merged, _ = read_config(str(cfg / "config.dev.yaml"))
    assert merged["server"]["monitor_port"] == 9000
    assert merged["server"]["extra"] == "x"


def test_no_merge_when_only_single_file_name(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    merged, _ = read_config(str(cfg / "config.yaml"))
    assert merged == {"a": 1}


def test_prod_without_base_uses_overlay_only(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.prod.yaml").write_text("k: v\n", encoding="utf-8")
    merged, _ = read_config(str(cfg / "config.prod.yaml"))
    assert merged == {"k": "v"}


def test_normalize_server_categorized_yaml() -> None:
    raw = {
        "architecture": {"monitor_port": 8765, "docs_port": 8767, "ops_port": 8768},
        "account": {"trading_port": 8769, "portfolio_port": 8771},
        "research": {"research_port": 8773, "market_port": 8772, "strategy_port": 8770},
        "feed": {"massive_port": 8766},
        "skip_monitor_ib": True,
    }
    flat = normalize_server_config(raw)
    assert "architecture" not in flat
    assert flat["monitor_port"] == 8765
    # Retired ports are accepted and dropped (TD-79); trading_port is now account_port.
    assert "massive_port" not in flat and "docs_port" not in flat
    assert flat["account_port"] == flat["trading_port"] == 8769
    assert flat["skip_monitor_ib"] is True


def test_normalize_server_legacy_port_key() -> None:
    flat = normalize_server_config(
        {
            "port": 9999,
            "docs_port": 8767,
            "ops_port": 8768,
            "trading_port": 8769,
            "portfolio_port": 8771,
            "research_port": 8773,
            "market_port": 8772,
            "strategy_port": 8770,
            "massive_port": 8766,
        }
    )
    assert flat["monitor_port"] == 9999
    assert "port" not in flat


def test_normalize_server_empty_raises() -> None:
    with pytest.raises(ValueError, match="config\\['server'\\]"):
        normalize_server_config({})
    with pytest.raises(ValueError, match="Missing required"):
        normalize_server_config({"architecture": {}})


def test_four_live_ports_are_enough() -> None:
    """Only api-monitor, api-account, api-market and api-research listen (TD-79)."""
    flat = normalize_server_config(
        {
            "architecture": {"monitor_port": 8765},
            "account": {"account_port": 8769},
            "research": {"research_port": 8773, "market_port": 8772},
        }
    )
    assert (flat["monitor_port"], flat["account_port"], flat["market_port"], flat["research_port"]) == (
        8765,
        8769,
        8772,
        8773,
    )
    assert flat["trading_port"] == 8769


def test_account_port_wins_over_the_legacy_name() -> None:
    flat = normalize_server_config(
        {
            "architecture": {"monitor_port": 8765},
            "account": {"account_port": 9000, "trading_port": 8769},
            "research": {"research_port": 8773, "market_port": 8772},
        }
    )
    assert flat["account_port"] == flat["trading_port"] == 9000


def test_a_missing_live_port_still_raises() -> None:
    with pytest.raises(ValueError, match="account_port"):
        normalize_server_config({"architecture": {"monitor_port": 8765}, "research": {"research_port": 1, "market_port": 2}})


def test_the_ib_block_is_optional() -> None:
    """No Trade process opens an IB socket; monitor refused to boot without ib.host (TD-79)."""
    from bifrost_core.config.yaml_config import get_effective_ib_config

    eff = get_effective_ib_config({})
    assert eff["ib2_host"] is None
    eff = get_effective_ib_config({"ib": {"secondary": {"ip": "192.0.2.33"}}})
    assert eff["ib2_host"] == "192.0.2.33"

