"""TD-31: the redis-ib names core uses match the IB Gateway plugin's.

The plugin is the writer of record for redis-ib; core (api / worker) reads its ticks,
option cache, account snapshot and health hashes, and sends Operator RPCs on its
command stream. Core cannot import the plugin, so both repos test against one list:

    tests/contracts/redis_ib_keys.json   (this repo: the canonical copy)
    bifrost-platform-plugin tests/contracts/redis_ib_keys.json   (byte-identical copy)

Core's values are checked against it here. The plugin checks its redis_keys.py against its
copy, and its copy against this one (that test fails, not skips, when this repo is not next
to it). If a name changes, change both repos and both copies in one go. Strings are
compared only: nothing here talks to Redis.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

import bifrost_core
from bifrost_core.core import redis_health_keys as health
from bifrost_core.core.realtime import ib_account_keys as account
from bifrost_core.core.realtime import ib_ingestor_keys as ingestor
from bifrost_core.core.realtime import redis_keys as quote_keys
from bifrost_core.ib_operator.config import effective_ib_operator_settings

MANIFEST = Path(__file__).resolve().parent / "contracts" / "redis_ib_keys.json"


def _load_manifest(path: Path) -> dict[str, object]:
    keys = json.loads(path.read_text(encoding="utf-8"))["keys"]
    return {k: tuple(v) if isinstance(v, list) else v for k, v in keys.items()}


# --- the plugin's redis_keys.py, as the shared manifest records it -----------------------
PLUGIN = _load_manifest(MANIFEST)

# Plugin names core does not use (the gateway's own bookkeeping). Listed so a new plugin
# name has to be placed here or in CORE_TO_PLUGIN on purpose.
PLUGIN_ONLY = {
    "IB_GATEWAY_HEALTH_PREFIX",  # per-account gateway health; core reads the ws_ib_* hashes
    "IB_GATEWAY_SELF_HEAL_KEY",  # gateway self-heal control, gateway-internal
    "IB_OPERATOR_CONSUMER_GROUP",  # the gateway's group; core only adds entries and reads results
}

_OPERATOR_DEFAULTS = effective_ib_operator_settings({})

# core value -> plugin name
CORE_TO_PLUGIN = [
    (health.BIFROST_HEALTH_IB_INGESTOR, "IB_INGESTER_HEALTH_KEY"),
    (ingestor.IB_INGESTER_META_HEALTH, "IB_INGESTER_HEALTH_KEY"),
    (ingestor.IB_INGESTER_CHANNEL, "IB_INGESTER_CHANNEL"),
    (quote_keys.SUBSCRIBE_CHANNEL_DEFAULT, "IB_INGESTER_CHANNEL"),
    (ingestor.IB_INGESTER_TICK_PREFIX, "IB_INGESTER_TICK_PREFIX"),
    (ingestor.IB_INGESTER_TICK_TTL_SEC, "IB_INGESTER_TICK_TTL_SEC"),
    (ingestor.IB_INGESTER_META_SUBSCRIPTIONS, "IB_INGESTER_SUBSCRIPTIONS_KEY"),
    (ingestor.IB_INGESTER_ON_DEMAND_STK, "IB_INGESTER_ON_DEMAND_STK"),
    (ingestor.IB_INGESTER_ON_DEMAND_STK_TS, "IB_INGESTER_ON_DEMAND_STK_TS"),
    (ingestor.ON_DEMAND_STK_DEFAULT_MAX_AGE_SEC, "ON_DEMAND_STK_DEFAULT_MAX_AGE_SEC"),
    (ingestor.IB_OPTION_CACHE_PREFIX, "IB_OPTION_CACHE_PREFIX"),
    (ingestor.IB_OPTION_CACHE_TTL_SEC, "IB_OPTION_CACHE_TTL_SEC"),
    (ingestor.IB_OPTION_ON_DEMAND_SET, "IB_OPTION_ON_DEMAND_SET"),
    (ingestor.IB_OPTION_ON_DEMAND_TS, "IB_OPTION_ON_DEMAND_TS"),
    (ingestor.IB_OPTION_CACHE_META_REFRESH_TS, "IB_OPTION_CACHE_META_REFRESH_TS"),
    (ingestor.ON_DEMAND_OPT_DEFAULT_MAX_AGE_SEC, "ON_DEMAND_OPT_DEFAULT_MAX_AGE_SEC"),
    (health.BIFROST_HEALTH_IB_ACCOUNT_AGENT, "IB_ACCOUNT_AGENT_HEALTH_KEY"),
    (account.IB_ACCOUNT_AGENT_META_HEALTH, "IB_ACCOUNT_AGENT_HEALTH_KEY"),
    (account.IB_ACCOUNT_SNAPSHOT_KEY, "IB_ACCOUNT_SNAPSHOT_KEY"),
    (account.IB_ACCOUNT_NOTIFY_CHANNEL, "IB_ACCOUNT_NOTIFY_CHANNEL"),
    (health.BIFROST_HEALTH_IB_OPERATOR, "IB_OPERATOR_HEALTH_KEY"),
    (_OPERATOR_DEFAULTS["health_key"], "IB_OPERATOR_HEALTH_KEY"),
    (_OPERATOR_DEFAULTS["stream"], "IB_OPERATOR_CMD_STREAM"),
    (_OPERATOR_DEFAULTS["result_prefix"], "IB_OPERATOR_RESULT_PREFIX"),
    (_OPERATOR_DEFAULTS["result_ttl_sec"], "IB_OPERATOR_RESULT_TTL_SEC"),
]


@pytest.mark.parametrize("core_value,plugin_name", CORE_TO_PLUGIN, ids=[p for _, p in CORE_TO_PLUGIN])
def test_core_value_matches_plugin(core_value: object, plugin_name: str) -> None:
    assert core_value == PLUGIN[plugin_name]


def test_every_plugin_name_is_placed() -> None:
    mapped = {p for _, p in CORE_TO_PLUGIN}
    placed_elsewhere = PLUGIN_ONLY | {"IB_OPERATOR_ENV_CMD_STREAMS", "STK_CONTRACT_KEY_SUFFIX"}
    assert set(PLUGIN) == mapped | placed_elsewhere
    assert not mapped & PLUGIN_ONLY


# --- per-env operator streams ---------------------------------------------------------
# Core takes the stream from `ib_operator.stream` in the env's config. Pinned from
# bifrost-trade-infra origin/main 76324b8 (2026-10-02):
#   k8s/overlays/dev/config/config.dev.yaml   ib_operator.stream (line 96)
#   k8s/overlays/stg/config/config.stg.yaml   ib_operator.stream (line 109; the later
#                                             `ib_operator:` block, which is the one YAML keeps)
#   k8s/overlays/prod/config/config.prod.yaml no ib_operator block -> core default
ENV_OPERATOR_BLOCKS = {
    "dev": {"ib_operator": {"enabled": True, "stream": "ib:operator:cmd:dev"}},
    "stg": {"ib_operator": {"stream": "ib:operator:cmd:stg"}},
    "prod": {},
}


@pytest.mark.parametrize("env", sorted(ENV_OPERATOR_BLOCKS))
def test_env_operator_stream_is_one_the_gateway_reads(env: str) -> None:
    stream = effective_ib_operator_settings(ENV_OPERATOR_BLOCKS[env])["stream"]
    allowed = {PLUGIN["IB_OPERATOR_CMD_STREAM"], *PLUGIN["IB_OPERATOR_ENV_CMD_STREAMS"]}
    assert stream in allowed


def test_non_prod_envs_use_their_own_stream() -> None:
    """DEV and STG must not share PROD's stream (TD-21): the plugin's per-env names."""
    dev = effective_ib_operator_settings(ENV_OPERATOR_BLOCKS["dev"])["stream"]
    stg = effective_ib_operator_settings(ENV_OPERATOR_BLOCKS["stg"])["stream"]
    prod = effective_ib_operator_settings(ENV_OPERATOR_BLOCKS["prod"])["stream"]
    assert (dev, stg) == PLUGIN["IB_OPERATOR_ENV_CMD_STREAMS"]
    assert prod == PLUGIN["IB_OPERATOR_CMD_STREAM"]


def test_core_consumer_group_default_is_not_the_gateways() -> None:
    """Known difference, harmless: core's `consumer_group` setting is never read.

    The gateway owns the group (`ib-gateway`); core's client only appends commands and
    reads `result_prefix` keys. If core ever starts reading the stream itself, it must
    use the plugin's group -- this test then has to change.
    """
    assert _OPERATOR_DEFAULTS["consumer_group"] == "ib-operator"
    assert _OPERATOR_DEFAULTS["consumer_group"] != PLUGIN["IB_OPERATOR_CONSUMER_GROUP"]
    src = Path(bifrost_core.__file__).resolve().parent
    readers = [
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if "consumer_group" in p.read_text(encoding="utf-8")
    ]
    assert readers == ["ib_operator/config.py"]


# --- ratchet: every redis-ib literal in core is in the contract -------------------------

# Core-only `ib:` literals, with why they are not in the plugin.
CORE_ONLY_IB_LITERALS = {
    "ib:ingester",  # IB_INGESTER_PREFIX: a namespace label, not a key
    "ib:operator:meta:health",  # legacy health key, read / config-normalisation fallback only
    "ib:ingester:meta:health",  # legacy health key, read fallback only
}


def _core_string_constants() -> set[str]:
    root = Path(bifrost_core.__file__).resolve().parent
    out: set[str] = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.add(node.value)
    return out


def test_every_ib_literal_in_core_is_in_the_contract() -> None:
    plugin_strings: set[str] = set()
    for value in PLUGIN.values():
        if isinstance(value, str):
            plugin_strings.add(value)
        elif isinstance(value, tuple):
            plugin_strings.update(value)
    ib_literals = {s for s in _core_string_constants() if s.startswith("ib:")}
    unknown = ib_literals - plugin_strings - CORE_ONLY_IB_LITERALS
    assert not unknown, f"redis-ib names core uses that the plugin does not define: {unknown}"


def test_stk_contract_key_suffix_matches_plugin() -> None:
    fragments = {s for s in _core_string_constants() if s.startswith("|STK")}
    assert fragments == {PLUGIN["STK_CONTRACT_KEY_SUFFIX"]}


# --- the plugin's copy of the manifest --------------------------------------------------
# The plugin's own test is the hard check (it fails when core is not next to it). Here the
# plugin is optional -- CI clones core alone -- so a missing sibling skips.
def _plugin_root() -> Path:
    env = os.environ.get("BIFROST_PLATFORM_PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "bifrost-platform-plugin"


def test_manifest_matches_plugin_copy() -> None:
    copy = _plugin_root() / "tests" / "contracts" / "redis_ib_keys.json"
    if not copy.is_file():
        pytest.skip(f"bifrost-platform-plugin not found at {copy.parents[2]}")
    assert copy.read_bytes() == MANIFEST.read_bytes(), (
        f"{copy} differs from {MANIFEST}: copy one onto the other and fix the code that breaks"
    )
