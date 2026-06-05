"""Redis-backed system message center for cross-service UI notifications."""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MESSAGE_CENTER_EVENTS_STREAM = "bifrost:msg:center:events"
MESSAGE_CENTER_INDEX_KEY = "bifrost:msg:center:index"
MESSAGE_CENTER_ITEM_PREFIX = "bifrost:msg:center:item:"
MESSAGE_CENTER_CONSUMER_PREFIX = "bifrost:msg:center:consumer:"
MESSAGE_CENTER_DEDUPE_PREFIX = "bifrost:msg:center:dedupe:"

MESSAGE_CENTER_TOPIC_IB_CONNECTION = "ib.connection"
MESSAGE_CENTER_TOPIC_PORTFOLIO_TWS_EXECUTIONS = "portfolio.tws_executions"
MESSAGE_CENTER_TOPIC_PORTFOLIO_FLEX_EXECUTIONS = "portfolio.flex_executions"
MESSAGE_CENTER_TTL_SEC = 3600
MESSAGE_CENTER_DEDUPE_WINDOW_SEC = 30
MESSAGE_CENTER_STREAM_MAXLEN = 4000
MESSAGE_CENTER_MONITOR_CONSUMER = "monitor_api"

IB_CONNECTION_LEVELS = {
    "connected": "success",
    "reconnecting": "warning",
    "disconnected": "error",
}

IB_CONNECTION_LABELS = {
    "connected": "Connected",
    "reconnecting": "Reconnecting",
    "disconnected": "Disconnected",
    "unknown": "Unknown",
}

IB_SERVICE_LABELS = {
    "ib_operator": "IB Operator",
    "ib_ingestor": "IB Ingestor",
    "ib_account_agent": "IB Account Agent",
    "portfolio_flex": "Portfolio (Flex)",
}


@dataclass(frozen=True)
class SystemMessageEvent:
    message_id: str
    topic: str
    level: str
    service: str
    slot: str
    client_id: Optional[int]
    account: Optional[str]
    status_from: str
    status_to: str
    title: str
    message: str
    reason: Optional[str]
    occurred_at: float
    dedupe_key: str


def _decode_value(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _item_key(message_id: str) -> str:
    return f"{MESSAGE_CENTER_ITEM_PREFIX}{message_id}"


def _consumer_key(consumer_name: str) -> str:
    return f"{MESSAGE_CENTER_CONSUMER_PREFIX}{consumer_name}:last_id"


def _dedupe_key(dedupe_key: str) -> str:
    return f"{MESSAGE_CENTER_DEDUPE_PREFIX}{dedupe_key}"


def _service_label(service: str) -> str:
    return IB_SERVICE_LABELS.get(service, service.replace("_", " ").title())


def _status_label(status: str) -> str:
    return IB_CONNECTION_LABELS.get(status, status.replace("_", " ").title())


def _sanitize_slot(slot: Optional[str]) -> str:
    s = str(slot or "host").strip().lower()
    return s or "host"


def _coerce_int(raw: Any) -> Optional[int]:
    try:
        if raw is None or str(raw).strip() == "":
            return None
        return int(raw)
    except (TypeError, ValueError):
        return None


def _coerce_float(raw: Any, default: float = 0.0) -> float:
    try:
        if raw is None or str(raw).strip() == "":
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def system_message_event_to_fields(event: SystemMessageEvent) -> Dict[str, str]:
    return {
        "message_id": event.message_id,
        "topic": event.topic,
        "level": event.level,
        "service": event.service,
        "slot": event.slot,
        "client_id": "" if event.client_id is None else str(int(event.client_id)),
        "account": "" if not event.account else str(event.account),
        "status_from": event.status_from,
        "status_to": event.status_to,
        "title": event.title,
        "message": event.message,
        "reason": "" if not event.reason else str(event.reason),
        "occurred_at": str(float(event.occurred_at)),
        "dedupe_key": event.dedupe_key,
    }


def parse_system_message_event(fields: Dict[Any, Any]) -> Optional[SystemMessageEvent]:
    topic = _decode_value(fields.get("topic") or fields.get(b"topic")).strip()
    if not topic:
        return None
    message_id = _decode_value(fields.get("message_id") or fields.get(b"message_id")).strip()
    if not message_id:
        return None
    return SystemMessageEvent(
        message_id=message_id,
        topic=topic,
        level=_decode_value(fields.get("level") or fields.get(b"level")).strip() or "info",
        service=_decode_value(fields.get("service") or fields.get(b"service")).strip(),
        slot=_sanitize_slot(fields.get("slot") or fields.get(b"slot")),
        client_id=_coerce_int(fields.get("client_id") or fields.get(b"client_id")),
        account=_decode_value(fields.get("account") or fields.get(b"account")).strip() or None,
        status_from=_decode_value(fields.get("status_from") or fields.get(b"status_from")).strip() or "unknown",
        status_to=_decode_value(fields.get("status_to") or fields.get(b"status_to")).strip() or "unknown",
        title=_decode_value(fields.get("title") or fields.get(b"title")).strip(),
        message=_decode_value(fields.get("message") or fields.get(b"message")).strip(),
        reason=_decode_value(fields.get("reason") or fields.get(b"reason")).strip() or None,
        occurred_at=_coerce_float(fields.get("occurred_at") or fields.get(b"occurred_at"), default=time.time()),
        dedupe_key=_decode_value(fields.get("dedupe_key") or fields.get(b"dedupe_key")).strip(),
    )


def publish_system_message_event(
    r: Any,
    event: SystemMessageEvent,
    *,
    maxlen: int = MESSAGE_CENTER_STREAM_MAXLEN,
) -> Optional[str]:
    try:
        return r.xadd(
            MESSAGE_CENTER_EVENTS_STREAM,
            system_message_event_to_fields(event),
            maxlen=maxlen,
            approximate=True,
        )
    except Exception as e:
        logger.warning("message center xadd failed topic=%s message_id=%s: %s", event.topic, event.message_id, e)
        return None


def build_ib_connection_event(
    *,
    service: str,
    slot: str,
    client_id: Optional[int],
    account: Optional[str],
    status_from: str,
    status_to: str,
    reason: Optional[str] = None,
    occurred_at: Optional[float] = None,
) -> SystemMessageEvent:
    slot_name = _sanitize_slot(slot)
    service_label = _service_label(service)
    from_label = _status_label(status_from)
    to_label = _status_label(status_to)
    client_part = f", client_id={client_id}" if client_id is not None else ""
    account_part = f", account={account}" if account else ""
    reason_part = f" ({reason})" if reason else ""
    title = f"{service_label} {slot_name} connection changed"
    message = (
        f"{service_label} ({slot_name}{client_part}{account_part}) changed: "
        f"{from_label} -> {to_label}{reason_part}"
    )
    return SystemMessageEvent(
        message_id=uuid.uuid4().hex,
        topic=MESSAGE_CENTER_TOPIC_IB_CONNECTION,
        level=IB_CONNECTION_LEVELS.get(status_to, "info"),
        service=service,
        slot=slot_name,
        client_id=client_id,
        account=account,
        status_from=status_from,
        status_to=status_to,
        title=title,
        message=message,
        reason=reason,
        occurred_at=float(occurred_at or time.time()),
        dedupe_key=f"{MESSAGE_CENTER_TOPIC_IB_CONNECTION}:{service}:{slot_name}:{client_id or 0}:{status_to}",
    )


def build_portfolio_tws_executions_fetch_event(
    *,
    ok: bool,
    title: str,
    message: str,
    reason: Optional[str] = None,
    level: Optional[str] = None,
    occurred_at: Optional[float] = None,
) -> SystemMessageEvent:
    """User-facing summary for POST /executions/fetch (TWS); detail goes in ``message`` / ``reason``."""
    lv = level or ("error" if not ok else "success")
    status_to = "complete" if ok else "failed"
    return SystemMessageEvent(
        message_id=uuid.uuid4().hex,
        topic=MESSAGE_CENTER_TOPIC_PORTFOLIO_TWS_EXECUTIONS,
        level=lv,
        service="ib_operator",
        slot="host",
        client_id=None,
        account=None,
        status_from="unknown",
        status_to=status_to,
        title=title,
        message=message,
        reason=reason,
        occurred_at=float(occurred_at or time.time()),
        dedupe_key=f"{MESSAGE_CENTER_TOPIC_PORTFOLIO_TWS_EXECUTIONS}:{uuid.uuid4().hex}",
    )


def build_portfolio_flex_executions_fetch_event(
    *,
    ok: bool,
    title: str,
    message: str,
    reason: Optional[str] = None,
    level: Optional[str] = None,
    occurred_at: Optional[float] = None,
) -> SystemMessageEvent:
    """User-facing summary for POST /executions/fetch-flex and fetch-flex-upload."""
    lv = level or ("error" if not ok else "success")
    status_to = "complete" if ok else "failed"
    return SystemMessageEvent(
        message_id=uuid.uuid4().hex,
        topic=MESSAGE_CENTER_TOPIC_PORTFOLIO_FLEX_EXECUTIONS,
        level=lv,
        service="portfolio_flex",
        slot="host",
        client_id=None,
        account=None,
        status_from="unknown",
        status_to=status_to,
        title=title,
        message=message,
        reason=reason,
        occurred_at=float(occurred_at or time.time()),
        dedupe_key=f"{MESSAGE_CENTER_TOPIC_PORTFOLIO_FLEX_EXECUTIONS}:{uuid.uuid4().hex}",
    )


class IbConnectionStatusTracker:
    """Publish IB status change events while suppressing repeated health writes."""

    def __init__(self, r: Any, *, service: str) -> None:
        self._r = r
        self._service = service
        self._last_status_by_slot: Dict[str, str] = {}

    def update(
        self,
        *,
        slot: str,
        status: str,
        client_id: Optional[int],
        account: Optional[str] = None,
        reason: Optional[str] = None,
        occurred_at: Optional[float] = None,
    ) -> Optional[str]:
        slot_name = _sanitize_slot(slot)
        next_status = str(status or "unknown").strip().lower() or "unknown"
        prev_status = self._last_status_by_slot.get(slot_name)
        self._last_status_by_slot[slot_name] = next_status
        if prev_status == next_status:
            return None
        if prev_status is None and next_status != "connected":
            return None
        event = build_ib_connection_event(
            service=self._service,
            slot=slot_name,
            client_id=client_id,
            account=account,
            status_from=prev_status or "unknown",
            status_to=next_status,
            reason=reason,
            occurred_at=occurred_at,
        )
        return publish_system_message_event(self._r, event)


def consumer_last_id(r: Any, consumer_name: str) -> str:
    try:
        raw = r.get(_consumer_key(consumer_name))
    except Exception:
        return "0-0"
    value = _decode_value(raw).strip()
    return value or "0-0"


def set_consumer_last_id(r: Any, consumer_name: str, last_id: str) -> None:
    try:
        r.set(_consumer_key(consumer_name), last_id)
    except Exception as e:
        logger.debug("message center save consumer offset failed (%s): %s", consumer_name, e)


def read_stream_events(
    r: Any,
    *,
    last_id: str,
    block_ms: int = 0,
    count: int = 100,
) -> List[tuple[str, Dict[Any, Any]]]:
    try:
        reply = r.xread({MESSAGE_CENTER_EVENTS_STREAM: last_id}, count=count, block=block_ms)
    except Exception as e:
        logger.debug("message center xread failed after %s: %s", last_id, e)
        return []
    if not reply:
        return []
    out: List[tuple[str, Dict[Any, Any]]] = []
    for _stream_name, entries in reply:
        for stream_id, fields in entries:
            out.append((_decode_value(stream_id), fields))
    return out


def materialize_stream_event(
    r: Any,
    event: SystemMessageEvent,
    *,
    ttl_sec: int = MESSAGE_CENTER_TTL_SEC,
    dedupe_window_sec: int = MESSAGE_CENTER_DEDUPE_WINDOW_SEC,
) -> Optional[Dict[str, Any]]:
    now = time.time()
    age_sec = max(0.0, now - float(event.occurred_at))
    if age_sec >= float(ttl_sec):
        return None
    ttl_remaining = max(1, int(math.ceil(float(ttl_sec) - age_sec)))
    body: Dict[str, Any] = {
        "message_id": event.message_id,
        "topic": event.topic,
        "level": event.level,
        "service": event.service,
        "slot": event.slot,
        "client_id": event.client_id,
        "account": event.account,
        "status_from": event.status_from,
        "status_to": event.status_to,
        "title": event.title,
        "message": event.message,
        "reason": event.reason,
        "occurred_at": float(event.occurred_at),
    }
    try:
        pipe = r.pipeline()
        if event.dedupe_key:
            prev_message_id = _decode_value(r.get(_dedupe_key(event.dedupe_key))).strip()
            if prev_message_id and prev_message_id != event.message_id:
                pipe.zrem(MESSAGE_CENTER_INDEX_KEY, prev_message_id)
                pipe.delete(_item_key(prev_message_id))
            pipe.set(
                _dedupe_key(event.dedupe_key),
                event.message_id,
                ex=max(1, min(int(dedupe_window_sec), ttl_remaining)),
            )
        pipe.set(_item_key(event.message_id), json.dumps(body, separators=(",", ":")), ex=ttl_remaining)
        pipe.zadd(MESSAGE_CENTER_INDEX_KEY, {event.message_id: float(event.occurred_at)})
        pipe.execute()
    except Exception as e:
        logger.warning("message center materialize failed message_id=%s: %s", event.message_id, e)
        return None
    return body


def sync_events_for_consumer(
    r: Any,
    *,
    consumer_name: str = MESSAGE_CENTER_MONITOR_CONSUMER,
    block_ms: int = 0,
    count: int = 100,
) -> List[Dict[str, Any]]:
    last_id = consumer_last_id(r, consumer_name)
    entries = read_stream_events(r, last_id=last_id, block_ms=block_ms, count=count)
    if not entries:
        return []
    emitted: List[Dict[str, Any]] = []
    newest_id = last_id
    for stream_id, fields in entries:
        newest_id = stream_id
        event = parse_system_message_event(fields)
        if event is None:
            continue
        message = materialize_stream_event(r, event)
        if message is not None:
            emitted.append(message)
    if newest_id != last_id:
        set_consumer_last_id(r, consumer_name, newest_id)
    return emitted


def fetch_materialized_messages(r: Any, *, limit: int = 20) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    stale_ids: List[str] = []
    start = 0
    remaining = max(1, int(limit))
    while len(out) < limit and start < limit * 4:
        ids = r.zrevrange(MESSAGE_CENTER_INDEX_KEY, start, start + remaining - 1)
        if not ids:
            break
        pipe = r.pipeline()
        for raw_id in ids:
            pipe.get(_item_key(_decode_value(raw_id)))
        payloads = pipe.execute()
        for raw_id, raw_payload in zip(ids, payloads):
            message_id = _decode_value(raw_id)
            if raw_payload is None:
                stale_ids.append(message_id)
                continue
            try:
                parsed = json.loads(_decode_value(raw_payload))
            except Exception:
                stale_ids.append(message_id)
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
                if len(out) >= limit:
                    break
        start += len(ids)
        remaining = max(1, limit - len(out))
    if stale_ids:
        try:
            r.zrem(MESSAGE_CENTER_INDEX_KEY, *stale_ids)
        except Exception:
            pass
    return out
