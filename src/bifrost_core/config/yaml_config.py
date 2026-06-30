"""YAML config load, path resolution, and IB flattening. Shared by daemon, server, Celery, and monitor."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from bifrost_core.core.dict_merge import deep_merge
from bifrost_core.config.connection_policy import merge_ib_policy_into_effective_ib

IB_PORT_MAP = {"tws_live": 7496, "tws_paper": 7497, "gateway": 4002}

# IB block shape is the same in dev/prod templates; error messages reference both.
_IB_YAML_EXAMPLE_HINT = (
    "See config/config.dev.yaml.example or config/config.prod.yaml.example for the `ib:` shape."
)

_VALID_ENV_NAMES = frozenset(("dev", "prod"))


def _server_int_opt(d: dict, key: str) -> Optional[int]:
    v = d.get(key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _validate_listen_port(name: str, value: int) -> None:
    if value < 1 or value > 65535:
        raise ValueError(f"server listen port {name} must be in 1..65535, got {value}")


def _required_listen_port(
    nested: dict,
    flat: dict,
    key: str,
    legacy_flat_key: Optional[str],
    yaml_hint: str,
) -> int:
    """Resolve one listen port from categorized nested dict and/or flat ``server`` keys (no defaults)."""
    v = _server_int_opt(nested, key)
    if v is not None:
        _validate_listen_port(yaml_hint, v)
        return v
    v = _server_int_opt(flat, key)
    if v is not None:
        _validate_listen_port(yaml_hint, v)
        return v
    if legacy_flat_key:
        v = _server_int_opt(flat, legacy_flat_key)
        if v is not None:
            _validate_listen_port(yaml_hint, v)
            return v
    if key in nested and nested[key] is not None:
        raise ValueError(
            f"{yaml_hint} must be an integer, got {nested[key]!r} "
            "(set a numeric port in YAML; see config/config.yaml.example)."
        )
    if key in flat and flat[key] is not None:
        raise ValueError(
            f"{yaml_hint} must be an integer, got {flat[key]!r} "
            "(set a numeric port in YAML; see config/config.yaml.example)."
        )
    if legacy_flat_key and legacy_flat_key in flat and flat[legacy_flat_key] is not None:
        raise ValueError(
            f"{yaml_hint} (legacy server.{legacy_flat_key}) must be an integer, "
            f"got {flat[legacy_flat_key]!r}"
        )
    raise ValueError(
        f"Missing required {yaml_hint} in config YAML. "
        "Define all sidecar listen ports under server.architecture, server.account, "
        "server.research, and server.feed (or equivalent flat keys). "
        "See config/config.yaml.example."
    )


def normalize_server_config(server: Optional[dict]) -> dict:
    """Flatten ``server.{architecture,account,research,feed}`` into canonical port keys.

    Categories align with API Health / Services Overview: Architecture, Account, Research, Feed.

    - ``architecture``: ``monitor_port`` (legacy top-level ``port`` also accepted), ``docs_port``, ``ops_port``
    - ``account``: ``trading_port``, ``portfolio_port``
    - ``research``: ``research_port``, ``market_port``, ``strategy_port``
    - ``feed``: ``massive_port``

    Nested values win over legacy flat keys. Category keys and legacy ``port`` are removed from the result.

    **All listen ports are required** — there are no code defaults; omitting a port raises ``ValueError``.
    """
    if not server or not isinstance(server, dict):
        raise ValueError(
            "config['server'] must be a YAML object with all listen ports. "
            "See config/config.yaml.example (server.architecture, server.account, "
            "server.research, server.feed)."
        )
    srv = dict(server)
    arch = srv.get("architecture") if isinstance(srv.get("architecture"), dict) else {}
    acc = srv.get("account") if isinstance(srv.get("account"), dict) else {}
    res = srv.get("research") if isinstance(srv.get("research"), dict) else {}
    feed = srv.get("feed") if isinstance(srv.get("feed"), dict) else {}

    monitor_port = _required_listen_port(arch, srv, "monitor_port", "port", "server.architecture.monitor_port")
    docs_port = _required_listen_port(arch, srv, "docs_port", None, "server.architecture.docs_port")
    ops_port = _required_listen_port(arch, srv, "ops_port", None, "server.architecture.ops_port")
    trading_port = _required_listen_port(acc, srv, "trading_port", None, "server.account.trading_port")
    portfolio_port = _required_listen_port(acc, srv, "portfolio_port", None, "server.account.portfolio_port")
    research_port = _required_listen_port(res, srv, "research_port", None, "server.research.research_port")
    market_port = _required_listen_port(res, srv, "market_port", None, "server.research.market_port")
    strategy_port = _required_listen_port(res, srv, "strategy_port", None, "server.research.strategy_port")
    massive_port = _required_listen_port(feed, srv, "massive_port", None, "server.feed.massive_port")

    out = {
        k: v
        for k, v in srv.items()
        if k not in ("architecture", "account", "research", "feed", "port")
    }
    out["monitor_port"] = monitor_port
    out["docs_port"] = docs_port
    out["ops_port"] = ops_port
    out["trading_port"] = trading_port
    out["portfolio_port"] = portfolio_port
    out["research_port"] = research_port
    out["market_port"] = market_port
    out["strategy_port"] = strategy_port
    out["massive_port"] = massive_port
    return out


def resolve_startup_config_path(project_root: str, argv: List[str]) -> Tuple[str, List[str]]:
    """Resolve config file for ``run_engine`` / ``run_server`` / ``run_celery``.

    Precedence:

    1. ``BIFROST_CONFIG`` environment variable (if non-empty): path used as-is (relative to project root if not absolute).
    2. First positional argument: explicit path to a YAML file.
    3. ``--env dev|prod``, ``--dev``, ``--prod``, or ``-e dev|prod`` (stripped from returned argv).
    4. ``BIFROST_ENV`` environment variable (default ``dev``).
    5. ``config/config.{dev|prod}.yaml`` under project root.

    If that file is missing, falls back to ``config/config.yaml``, then ``config/config.yaml.example``.

    Returns ``(absolute_config_path, argv_without_consumed_env_flags_and_explicit_config_path)``.
    Unknown flags (e.g. ``--debug``) are preserved in the second return value.
    """
    root = Path(project_root)

    if os.environ.get("BIFROST_CONFIG", "").strip():
        raw = os.environ["BIFROST_CONFIG"].strip()
        p = Path(raw)
        if not p.is_absolute():
            p = root / raw
        return str(p.resolve()), list(argv)

    i = 0
    env_from_flag: Optional[str] = None
    stripped: List[str] = []
    while i < len(argv):
        a = argv[i]
        if a in ("--env", "-e") and i + 1 < len(argv):
            v = argv[i + 1].lower().strip()
            env_from_flag = v if v in _VALID_ENV_NAMES else "dev"
            i += 2
            continue
        if a.startswith("--env="):
            v = a.split("=", 1)[1].lower().strip()
            env_from_flag = v if v in _VALID_ENV_NAMES else "dev"
            i += 1
            continue
        if a == "--prod":
            env_from_flag = "prod"
            i += 1
            continue
        if a == "--dev":
            env_from_flag = "dev"
            i += 1
            continue
        stripped.append(a)
        i += 1

    explicit_path: Optional[str] = None
    rest: List[str] = []
    for a in stripped:
        if a.startswith("--"):
            rest.append(a)
            continue
        if explicit_path is None:
            explicit_path = a
        else:
            rest.append(a)

    if explicit_path:
        p = Path(explicit_path)
        if not p.is_absolute():
            p = root / explicit_path
        return str(p.resolve()), rest

    env_name = (env_from_flag or os.environ.get("BIFROST_ENV", "dev") or "dev").lower().strip()
    if env_name not in _VALID_ENV_NAMES:
        env_name = "dev"

    candidate = root / "config" / f"config.{env_name}.yaml"
    if candidate.exists():
        return str(candidate.resolve()), rest

    legacy = root / "config" / "config.yaml"
    if legacy.exists():
        return str(legacy.resolve()), rest

    return str((root / "config" / "config.yaml.example").resolve()), rest


def config_profile_from_resolved_path(resolved_path: str) -> Optional[str]:
    """Return ``dev`` or ``prod`` when the loaded file is ``config.dev.yaml`` / ``config.prod.yaml``.

    Used for UI (e.g. browser tab title). Custom paths or ``config.yaml`` alone return ``None``."""
    name = Path(resolved_path).name
    if name == "config.dev.yaml":
        return "dev"
    if name == "config.prod.yaml":
        return "prod"
    return None


def ops_api_console_stream_key(profile: Optional[str]) -> str:
    """Redis Stream key for Ops API application logs (api_ops console).

    ``run_server_ops.py`` and Monitor ``/api/ops/logs*`` use ``bifrost:console:{dev|prod}:api_ops``
    so Dev and Prod processes sharing one Redis do not mix lines. Non-``prod`` profiles (including
    ``None`` for plain ``config.yaml``) map to the ``dev`` stream."""
    suffix = "prod" if profile == "prod" else "dev"
    return f"bifrost:console:{suffix}:api_ops"


def docs_api_console_stream_key(profile: Optional[str]) -> str:
    """Redis Stream key for Docs API application logs (api_docs console).

    ``run_server_docs.py`` and Monitor ``/api/docs/logs*`` use ``bifrost:console:{dev|prod}:api_docs``.
    Non-``prod`` profiles map to the ``dev`` stream."""
    suffix = "prod" if profile == "prod" else "dev"
    return f"bifrost:console:{suffix}:api_docs"


def monitor_api_console_stream_key(profile: Optional[str]) -> str:
    """Redis Stream key for Monitor API application logs (api_monitor console).

    ``run_server.py`` and Monitor ``/api/monitor/logs*`` use ``bifrost:console:{dev|prod}:api_monitor``
    so Dev and Prod processes sharing one Redis do not mix lines. Non-``prod`` profiles (including
    ``None`` for plain ``config.yaml``) map to the ``dev`` stream."""
    suffix = "prod" if profile == "prod" else "dev"
    return f"bifrost:console:{suffix}:api_monitor"


def trading_api_console_stream_key(profile: Optional[str]) -> str:
    """Redis Stream for Trading API console (``run_server_trading.py`` → Monitor ``/api/trading/logs*``)."""
    suffix = "prod" if profile == "prod" else "dev"
    return f"bifrost:console:{suffix}:api_trading"


def portfolio_api_console_stream_key(profile: Optional[str]) -> str:
    """Redis Stream for Portfolio API console (``run_server_portfolio.py`` → Monitor ``/api/portfolio/logs*``)."""
    suffix = "prod" if profile == "prod" else "dev"
    return f"bifrost:console:{suffix}:api_portfolio"


def research_api_console_stream_key(profile: Optional[str]) -> str:
    """Redis Stream for Research API console (``run_server_research.py`` → Monitor ``/api/research/logs*``)."""
    suffix = "prod" if profile == "prod" else "dev"
    return f"bifrost:console:{suffix}:api_research"


def strategy_api_console_stream_key(profile: Optional[str]) -> str:
    """Redis Stream for Strategy API console (``run_server_strategy.py`` → Monitor ``/api/strategy/logs*``)."""
    suffix = "prod" if profile == "prod" else "dev"
    return f"bifrost:console:{suffix}:api_strategy"


def market_api_console_stream_key(profile: Optional[str]) -> str:
    """Redis Stream for Market API console (``run_server_market.py`` → Monitor ``/api/market/logs*``)."""
    suffix = "prod" if profile == "prod" else "dev"
    return f"bifrost:console:{suffix}:api_market"


def daemon_trading_console_stream_key(profile: Optional[str]) -> str:
    """Redis Stream for Strategy Trading Daemon console (``run_engine.py`` → Monitor ``/api/daemon/logs*``).

    Same dev|prod split as API monitor/ops logs so Dev and Prod engines sharing one Redis do not
    overwrite each other's lines. Non-``prod`` profiles map to the ``dev`` stream."""
    suffix = "prod" if profile == "prod" else "dev"
    return f"bifrost:console:{suffix}:daemon_trading"


def read_config(config_path: Optional[str] = None) -> tuple[dict, str]:
    """Load YAML. Returns (config, resolved_path).

    When the resolved file is ``config/config.dev.yaml`` or ``config/config.prod.yaml`` and
    ``config/config.yaml`` exists in the same directory, load ``config.yaml`` first and **deep-merge**
    the env-specific file on top (overlay wins). This matches a split where shared keys live in
    ``config.yaml`` and env overrides live in ``config.dev.yaml`` / ``config.prod.yaml``.

    After merge, ``server`` is passed through :func:`normalize_server_config` so categorized YAML
    (``architecture`` / ``account`` / ``research`` / ``feed``) becomes flat keys (``monitor_port``, …).
    """
    config_path = config_path or os.environ.get("BIFROST_CONFIG", "config/config.yaml")
    if not Path(config_path).exists():
        config_path = "config/config.yaml.example"
    config_path = str(Path(config_path).resolve())
    path_obj = Path(config_path)
    name = path_obj.name

    with open(config_path, "r", encoding="utf-8") as f:
        overlay: Dict[str, Any] = yaml.safe_load(f) or {}

    if name in ("config.dev.yaml", "config.prod.yaml"):
        base_path = path_obj.parent / "config.yaml"
        if base_path.is_file():
            with open(base_path, "r", encoding="utf-8") as f:
                base: Dict[str, Any] = yaml.safe_load(f) or {}
            merged = deep_merge(base, overlay)
        else:
            merged = overlay
    else:
        merged = overlay

    if isinstance(merged.get("server"), dict):
        merged["server"] = normalize_server_config(merged["server"])
    return merged, config_path


def _flatten_host_secondary_ib(ib: dict) -> Dict[str, Any]:
    """Map ``ib.host`` / ``ib.secondary`` to internal flat keys for port resolution and connectors.

    Primary TWS: ``ib.host.ip``, ``ib.host.port_type``, ``ib.host.client_id.*``
    Optional second TWS: ``ib.secondary`` (ip, port_type, client_id.listener/operator).
    """
    h = ib.get("host")
    if not isinstance(h, dict):
        raise ValueError(
            "config['ib']['host'] is required (dict with ip, port_type, client_id). "
            + _IB_YAML_EXAMPLE_HINT
        )
    s = ib.get("secondary") if isinstance(ib.get("secondary"), dict) else {}
    hc = h.get("client_id") if isinstance(h.get("client_id"), dict) else {}
    sc = s.get("client_id") if isinstance(s.get("client_id"), dict) else {}

    def _market_gateway_cid(client_ids: dict) -> int:
        if client_ids.get("market_gateway") is not None:
            return int(client_ids["market_gateway"])
        for key in ("ingestor", "ib_market_ingest"):
            if client_ids.get(key) is not None:
                return int(client_ids[key])
        return 150

    market_cid = _market_gateway_cid(hc)
    merged_market = hc.get("market_gateway") is not None

    host = str(h.get("ip") or "127.0.0.1").strip()
    ptp = str(h.get("port_type") or "tws_paper").strip().lower()
    # Optional: market data / IB ingestor use this port (host IB only). Empty = same as port_type.
    ptp_md = str(h.get("port_type_market_data") or "").strip().lower()
    ib2_h = str(s.get("ip") or "").strip()
    ib2_pt = str(s.get("port_type") or "tws_paper").strip().lower()

    return {
        "host": host,
        "port_type": ptp,
        "port_type_market_data": ptp_md or None,
        "ib2_host": ib2_h or None,
        "ib2_port_type": ib2_pt,
        "connect_timeout": ib.get("connect_timeout"),
        "client_id_daemon": int(hc.get("daemon") or 1),
        "client_id_market_gateway": market_cid,
        "client_id_listener": market_cid if merged_market else int(hc.get("listener") or 2),
        # Host IB Operator (cmd RPC): YAML key `operator`; legacy `account` accepted for migration.
        "client_id_operator": int(hc.get("operator") or hc.get("account") or 100),
        "client_id_worker_market": market_cid if merged_market else int(hc.get("worker_market") or 500),
        # IB market gateway (W6): YAML `market_gateway`; legacy `ingestor` / `ib_market_ingest`.
        "client_id_ib_ingestor": market_cid,
        # IB Account Agent (run_ib_account_agent.py): account-domain IB events → Redis only.
        "client_id_account_agent": int(hc.get("account_agent") or 151),
        "ib2_client_id_listener": int(sc.get("listener") or 3),
        # Secondary IB Operator (same role as host operator); legacy YAML key `account` accepted.
        "ib2_client_id_operator": int(sc.get("operator") or sc.get("account") or 102),
        "ib2_client_id_account_agent": int(sc.get("account_agent") or 152),
    }


def get_effective_ib_config(config: dict) -> Dict[str, Any]:
    """Build the canonical IB connection dict from config.yaml (single source of truth for client IDs).

    Required shape::

        ib:
          connect_timeout: 60
          host:
            ip: ...
            port_type: ...
            client_id: { daemon, listener, operator, worker_market, ingestor, account_agent }
          secondary:  # optional second TWS
            ip: ...
            port_type: ...
            client_id: { listener, operator }

    See ``config/config.dev.yaml.example`` or ``config/config.prod.yaml.example`` (same ``ib`` shape).

    Returns a dict with normalised keys consumed by daemon, server, and celery:
      host, port_type, port, connect_timeout,
      client_id_daemon, client_id_listener, client_id_operator, client_id_worker_market,
      client_id_ib_ingestor,
      ib2_host, ib2_port_type, ib2_port, ib2_client_id_listener, ib2_client_id_operator.
    Also includes ``ib_*`` prefixed duplicates (``ib_host``, ``ib_client_id_daemon``, …) for internal callers.
    Monitor HTTP responses use ``src.monitor.reader.ib_config_public.ib_client_for_api`` (Settings-aligned names).
    """
    ib_raw = config.get("ib")
    if not ib_raw or not isinstance(ib_raw, dict):
        raise ValueError("config['ib'] is required in YAML. " + _IB_YAML_EXAMPLE_HINT)
    ib = _flatten_host_secondary_ib(ib_raw)
    host = str(ib.get("host") or "127.0.0.1").strip()
    port_type = str(ib.get("port_type") or "tws_paper").strip().lower()
    if port_type not in IB_PORT_MAP:
        port_type = "tws_paper"
    port = IB_PORT_MAP[port_type]

    ptm_raw = ib.get("port_type_market_data")
    if ptm_raw:
        ptm = str(ptm_raw).strip().lower()
        if ptm not in IB_PORT_MAP:
            ptm = port_type
    else:
        ptm = port_type
    port_market_data = IB_PORT_MAP[ptm]

    cid_d = int(ib.get("client_id_daemon") or 1)
    cid_mg = int(ib.get("client_id_market_gateway") or ib.get("client_id_ib_ingestor") or 150)
    cid_l = int(ib.get("client_id_listener") or 2)
    cid_op = int(ib.get("client_id_operator") or 100)
    cid_w = int(ib.get("client_id_worker_market") or 500)
    cid_mi = int(ib.get("client_id_ib_ingestor") or cid_mg)
    cid_aa = int(ib.get("client_id_account_agent") or 151)

    out: Dict[str, Any] = {
        "host": host,
        "port_type": port_type,
        "port": port,
        "port_type_market_data": ptm,
        "port_market_data": port_market_data,
        "connect_timeout": float(ib.get("connect_timeout") or 60.0),
        "client_id_daemon": cid_d,
        "client_id_market_gateway": cid_mg,
        "client_id_listener": cid_l,
        "client_id_operator": cid_op,
        "client_id_worker_market": cid_w,
        "client_id_ib_ingestor": cid_mi,
        "client_id_account_agent": cid_aa,
        # API / frontend aliases (ib_* prefix)
        "ib_host": host,
        "ib_port_type": port_type,
        "ib_port": port,
        "ib_port_type_market_data": ptm,
        "ib_port_market_data": port_market_data,
        "ib_client_id_daemon": cid_d,
        "ib_client_id_market_gateway": cid_mg,
        "ib_client_id_listener": cid_l,
        "ib_client_id_operator": cid_op,
        "ib_client_id_worker_market": cid_w,
        "ib_client_id_ib_ingestor": cid_mi,
        "ib_client_id_account_agent": cid_aa,
    }

    # Second IB
    ib2_host = str(ib.get("ib2_host") or "").strip()
    if ib2_host:
        ib2_pt = str(ib.get("ib2_port_type") or "tws_paper").strip().lower()
        if ib2_pt not in IB_PORT_MAP:
            ib2_pt = "tws_paper"
        ib2_cid_l = int(ib.get("ib2_client_id_listener") or 3)
        ib2_cid_op = int(ib.get("ib2_client_id_operator") or 102)
        ib2_cid_aa = int(ib.get("ib2_client_id_account_agent") or 152)
        out.update({
            "ib2_host": ib2_host,
            "ib2_port_type": ib2_pt,
            "ib2_port": IB_PORT_MAP[ib2_pt],
            "ib2_client_id_listener": ib2_cid_l,
            "ib2_client_id_operator": ib2_cid_op,
            "ib2_client_id_account_agent": ib2_cid_aa,
        })
    else:
        out.update({
            "ib2_host": None,
            "ib2_port_type": None,
            "ib2_port": None,
            "ib2_client_id_listener": 3,
            "ib2_client_id_operator": 102,
            "ib2_client_id_account_agent": 152,
        })

    merge_ib_policy_into_effective_ib(out, config)
    return out
