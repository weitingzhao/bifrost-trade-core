"""One answer to "which environment is this" (debt TD-52)."""

from __future__ import annotations

import pytest

from bifrost_core.config.profile import deployment_profile, profile_from_config_path
from bifrost_core.core.ops_lease import ops_profile_from_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIFROST_ENV", raising=False)
    monkeypatch.delenv("BIFROST_OPS_CONTROL_PROFILE", raising=False)


@pytest.mark.parametrize("env", ["dev", "stg", "prod"])
def test_control_profile_names_every_env(env: str) -> None:
    assert deployment_profile({"ops": {"control_profile": env}}) == env
    assert ops_profile_from_config({"ops": {"control_profile": env}}) == env


def test_control_profile_beats_a_stale_bifrost_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every K3s pod carried BIFROST_ENV=stg; the overlay's control_profile was right."""
    monkeypatch.setenv("BIFROST_ENV", "stg")
    assert deployment_profile({"ops": {"control_profile": "prod"}}, "/app/config/config.stg.yaml") == "prod"


def test_env_then_file_name(monkeypatch: pytest.MonkeyPatch) -> None:
    assert deployment_profile({}, "/x/config.prod.yaml") == "prod"
    monkeypatch.setenv("BIFROST_ENV", "stg")
    assert deployment_profile({}, "/x/config.prod.yaml") == "stg"
    monkeypatch.setenv("BIFROST_OPS_CONTROL_PROFILE", "dev")
    assert deployment_profile({}, "/x/config.prod.yaml") == "dev"


def test_unknown_values_are_none() -> None:
    assert deployment_profile({"ops": {"control_profile": "staging"}}) is None
    assert deployment_profile(None, "/app/config/runtime.yaml") is None


@pytest.mark.parametrize(
    "path, profile",
    [
        ("/x/config.dev.yaml", "dev"),
        ("/x/config.stg.yaml", "stg"),
        ("/x/config.prod.yaml", "prod"),
        ("/x/config.yaml", None),
        ("/x/config.yaml.example", None),
        ("/x/runtime.yaml", None),
        (None, None),
    ],
)
def test_profile_from_config_path(path: str, profile: str) -> None:
    assert profile_from_config_path(path) == profile
