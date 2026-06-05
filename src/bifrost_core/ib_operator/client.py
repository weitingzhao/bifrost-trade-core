"""Call IB Operator from API processes via Redis (sync + async helper)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional

import redis

from bifrost_core.config.startup import get_effective_ib_config
from bifrost_core.ib_operator.config import effective_ib_operator_settings
from bifrost_core.ib_operator.health_redis import (
    jsonish_connected,
    normalize_operator_health_payload,
    operator_health_dict_from_redis_hash,
)
from bifrost_core.ib_operator.protocol import PROTOCOL_VERSION, new_req_id, result_key
from bifrost_core.ib_operator.ib_probe_derived import (
    attach_ib_probe_derived,
    attach_service_heartbeat_derived,
)

logger = logging.getLogger(__name__)


class IbOperatorClient:
    """Publish commands to the operator stream and poll for result keys."""

    def __init__(
        self,
        *,
        redis_url: str,
        stream: str,
        result_prefix: str,
        default_timeout_sec: float = 120.0,
    ) -> None:
        self._redis_url = redis_url
        self._stream = stream
        self._result_prefix = result_prefix
        self._default_timeout_sec = float(default_timeout_sec)
        self._r: Optional[redis.Redis] = None

    @classmethod
    def from_merged_config(cls, config: Dict[str, Any]) -> Optional["IbOperatorClient"]:
        """Return client if operator + Redis are enabled; else None."""
        if (config.get("server") or {}).get("skip_monitor_ib", False):
            return None
        s = effective_ib_operator_settings(config)
        if not s["enabled"] or not s["redis_url"]:
            return None
        return cls(
            redis_url=s["redis_url"],
            stream=s["stream"],
            result_prefix=s["result_prefix"],
            default_timeout_sec=s["request_timeout_sec"],
        )

    def _conn(self) -> redis.Redis:
        if self._r is None:
            self._r = redis.from_url(self._redis_url, decode_responses=True)
        return self._r

    def close(self) -> None:
        if self._r is not None:
            try:
                self._r.close()
            except Exception:
                pass
            self._r = None

    def request(
        self,
        op: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        timeout_sec: Optional[float] = None,
        caller: str = "fastapi",
    ) -> Dict[str, Any]:
        """Blocking request. Returns envelope ``{ok, error?, data?}``."""
        payload = payload or {}
        timeout = float(timeout_sec if timeout_sec is not None else self._default_timeout_sec)
        if timeout <= 0:
            return {"ok": False, "error": "invalid_timeout"}

        r = self._conn()
        req_id = new_req_id()
        deadline_ms = int(time.time() * 1000 + timeout * 1000)
        fields = {
            "req_id": req_id,
            "v": PROTOCOL_VERSION,
            "op": op,
            "payload": json.dumps(payload, separators=(",", ":"), default=str),
            "caller": caller,
            "deadline_ms": str(deadline_ms),
        }
        try:
            r.xadd(self._stream, fields)
        except Exception as e:
            logger.warning("ib_operator xadd failed: %s", e)
            return {"ok": False, "error": f"redis_xadd:{e}"}

        rk = result_key(self._result_prefix, req_id)
        poll_interval = 0.05
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = r.get(rk)
            except Exception as e:
                return {"ok": False, "error": f"redis_get:{e}"}
            if raw:
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return {"ok": False, "error": "invalid_result_json"}
            time.sleep(poll_interval)

        return {"ok": False, "error": "timeout_waiting_for_operator"}

    async def request_async(
        self,
        op: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        timeout_sec: Optional[float] = None,
        caller: str = "fastapi",
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.request,
            op,
            payload,
            timeout_sec=timeout_sec,
            caller=caller,
        )


def read_operator_health(redis_url: str, health_key: str) -> Optional[Dict[str, Any]]:
    """Read operator health: Redis Hash (ingest-style string fields) or legacy JSON string value."""

    def _read_at_key(r: Any, key: str) -> Optional[Dict[str, Any]]:
        kt = r.type(key)
        if kt == "none":
            return None
        if kt == "hash":
            raw_map = r.hgetall(key)
            return operator_health_dict_from_redis_hash(raw_map or {})
        if kt == "string":
            raw = r.get(key)
            if not raw:
                return None
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                return normalize_operator_health_payload(loaded)
            return None
        return None

    try:
        r = redis.from_url(redis_url, decode_responses=True)
        try:
            return _read_at_key(r, health_key)
        finally:
            r.close()
    except Exception:
        return None


def build_monitor_ib_status(
    config: Dict[str, Any],
    ib_cfg: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Build IB Operator Redis health dict for GET /status ``socket.ib_operator`` (host + optional secondary)."""
    if (config.get("server") or {}).get("skip_monitor_ib", False):
        return None
    s = effective_ib_operator_settings(config)
    if not s["enabled"] or not s["redis_url"]:
        return None
    ib = ib_cfg or {}
    health = read_operator_health(s["redis_url"], s["health_key"])
    if health:
        health = normalize_operator_health_payload(health)
    unreachable = "IB Operator unreachable (is scripts/systemd/run_ib_operator.py running?)"
    try:
        _eff = get_effective_ib_config(config)
        _stale_m = float(_eff.get("ib_probe_stale_multiplier") or 2.5)
    except Exception:
        _stale_m = 2.5
    _now_ts = time.time()

    def _slot_from_health_dict(h: dict, cid_i: int) -> Dict[str, Any]:
        slot: Dict[str, Any] = {
            "connected": jsonish_connected(h.get("connected")),
            "client_id": int(h.get("client_id") or cid_i),
            "last_error": h.get("last_error"),
            "reconnects": int(h.get("reconnects") or 0),
        }
        attach_ib_probe_derived(
            slot,
            probe_at=float(h.get("ib_probe_at") or 0),
            probe_interval=float(h.get("ib_probe_interval_sec") or 0),
            probe_ok=jsonish_connected(h.get("ib_probe_ok")),
            stale_mult=_stale_m,
            now=_now_ts,
        )
        return slot

    def _slot(
        key: str,
        cid_key: str,
        *,
        fallback_err: Optional[str],
    ) -> Dict[str, Any]:
        cid = ib.get(cid_key)
        try:
            cid_i = int(cid) if cid is not None else 0
        except (TypeError, ValueError):
            cid_i = 0
        if health:
            h = health.get(key)
            if not isinstance(h, dict):
                if key == "host" and isinstance(health.get("operator"), dict):
                    h = health["operator"]
                elif key == "secondary" and isinstance(health.get("account2"), dict):
                    h = health["account2"]
            if isinstance(h, dict):
                return _slot_from_health_dict(h, cid_i)
        return {
            "connected": False,
            "client_id": cid_i,
            "last_error": unreachable if not health else fallback_err,
            "reconnects": 0,
        }

    out: Dict[str, Any] = {
        "host": _slot("host", "ib_client_id_operator", fallback_err=None),
    }
    ib2_host = ib.get("ib2_host") or ""
    ib2_host = ib2_host.strip() if isinstance(ib2_host, str) else ""
    try:
        cid2 = int(ib.get("ib2_client_id_operator") or 102)
    except (TypeError, ValueError):
        cid2 = 102
    if ib2_host or cid2 != 102:
        sec = health.get("secondary") if health else None
        if sec is None and health and isinstance(health.get("account2"), dict):
            sec = health["account2"]
        if health and sec is not None and isinstance(sec, dict):
            a2 = sec
            out["secondary"] = _slot_from_health_dict(a2, cid2)
        else:
            out["secondary"] = {
                "connected": False,
                "client_id": cid2,
                "last_error": unreachable
                if not health
                else ("Set Second IB host in Settings to enable" if not ib2_host else None),
                "reconnects": 0,
            }
    # Mirror ib_ingestor: top-level `connected` = Host slot (primary cmd RPC).
    out["connected"] = jsonish_connected(out["host"]["connected"])
    if health:
        out["service_alive"] = jsonish_connected(health.get("service_alive", True))
        out["operator_alive"] = out["service_alive"]
        try:
            out["msg_count"] = int(health.get("msg_count") or 0)
        except (TypeError, ValueError):
            out["msg_count"] = 0
        try:
            lm = float(health.get("last_msg_ts") or 0)
            out["last_msg_age_s"] = max(0.0, time.time() - lm) if lm > 0 else None
        except (TypeError, ValueError):
            out["last_msg_age_s"] = None
        out["reconnects"] = int(out["host"].get("reconnects") or 0)
        try:
            _sh_iv = float(health.get("service_heartbeat_interval_sec") or 0)
            _sh_last = float(health.get("last_service_heartbeat_at") or 0)
        except (TypeError, ValueError):
            _sh_iv = 0.0
            _sh_last = 0.0
        if _sh_iv > 0:
            attach_service_heartbeat_derived(
                out,
                interval_sec=_sh_iv,
                last_heartbeat_at=_sh_last,
                now=_now_ts,
            )
        _shr = (health.get("service_heartbeat_reconnect_in_progress") or "").strip()
        out["service_heartbeat_reconnect_in_progress"] = _shr if _shr else None
    else:
        out["service_alive"] = False
        out["operator_alive"] = False
        out["msg_count"] = 0
        out["last_msg_age_s"] = None
        out["reconnects"] = 0
        out["service_heartbeat_reconnect_in_progress"] = None
    return out
