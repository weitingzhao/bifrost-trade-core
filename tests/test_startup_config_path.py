"""resolve_startup_config_path for run_engine / run_server / run_celery."""

import os
from pathlib import Path

import pytest

from bifrost_core.config.startup import (
    config_profile_from_resolved_path,
    monitor_api_console_stream_key,
    ops_api_console_stream_key,
    read_config,
    resolve_startup_config_path,
)


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def test_resolve_default_dev(project_root: Path) -> None:
    os.environ.pop("BIFROST_CONFIG", None)
    os.environ.pop("BIFROST_ENV", None)
    p, rest = resolve_startup_config_path(str(project_root), [])
    assert "config.dev.yaml" in p or "config.yaml" in p
    assert rest == []


def test_resolve_prod_flag(project_root: Path) -> None:
    os.environ.pop("BIFROST_CONFIG", None)
    os.environ.pop("BIFROST_ENV", None)
    p, rest = resolve_startup_config_path(str(project_root), ["--prod"])
    assert "config.prod.yaml" in p
    assert rest == []


def test_resolve_explicit_path(project_root: Path) -> None:
    os.environ.pop("BIFROST_CONFIG", None)
    explicit = str(project_root / "config" / "config.yaml.example")
    p, rest = resolve_startup_config_path(str(project_root), [explicit])
    assert p.endswith("config.yaml.example")
    assert rest == []


def test_config_profile_from_resolved_path() -> None:
    assert config_profile_from_resolved_path("/x/config/config.dev.yaml") == "dev"
    assert config_profile_from_resolved_path("/x/config/config.prod.yaml") == "prod"
    assert config_profile_from_resolved_path("/x/config/config.yaml") is None
    assert config_profile_from_resolved_path("/custom/other.yaml") is None


def test_ops_api_console_stream_key() -> None:
    assert ops_api_console_stream_key("prod") == "bifrost:console:prod:api_ops"
    assert ops_api_console_stream_key("dev") == "bifrost:console:dev:api_ops"
    assert ops_api_console_stream_key(None) == "bifrost:console:dev:api_ops"


def test_monitor_api_console_stream_key() -> None:
    assert monitor_api_console_stream_key("prod") == "bifrost:console:prod:api_monitor"
    assert monitor_api_console_stream_key("dev") == "bifrost:console:dev:api_monitor"
    assert monitor_api_console_stream_key(None) == "bifrost:console:dev:api_monitor"


def test_bifrost_config_env_wins(project_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = str(project_root / "config" / "config.yaml.example")
    monkeypatch.setenv("BIFROST_CONFIG", target)
    p, rest = resolve_startup_config_path(str(project_root), ["--prod", "ignored"])
    assert p == str(Path(target).resolve())
    assert rest == ["--prod", "ignored"]


def test_read_config_dev_has_no_retired_ops_celery_fields(
    project_root: Path, tmp_path: Path
) -> None:
    """Wave 6.1: example config must not ship retired Celery worker_profiles / ops.celery blocks."""
    import shutil

    import yaml

    example = project_root / "config" / "config.yaml.example"
    dev_src = project_root / "config" / "config.dev.yaml"
    if not example.is_file() or not dev_src.is_file():
        pytest.skip("config.yaml.example / config.dev.yaml not present")

    example_cfg = yaml.safe_load(example.read_text(encoding="utf-8")) or {}
    example_ops = example_cfg.get("ops") or {}
    assert "worker_profiles" not in example_ops
    assert "celery" not in example_ops
    assert "celery_inspect_timeout_sec" not in example_ops
    assert "celery_inspect_wall_sec" not in example_ops

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    shutil.copy(example, cfg_dir / "config.yaml")
    shutil.copy(dev_src, cfg_dir / "config.dev.yaml")
    cfg, _ = read_config(str(cfg_dir / "config.dev.yaml"))
    ops = cfg.get("ops") or {}
    assert "worker_profiles" not in ops
    assert "celery" not in ops
