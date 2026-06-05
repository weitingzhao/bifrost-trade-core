"""Redis SUBSCRIBE loop for quote pub/sub (Market SSE). Uses a separate connection from the reader GET client."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict

from .redis_quotes import RedisQuotesReader

logger = logging.getLogger(__name__)

# Minimum interval (seconds) between broadcasts for the same contract_key.
# Reduces redundant Redis GETs when the ingestor publishes many messages per second
# for the same symbol (typical during active trading hours).
_MIN_BROADCAST_INTERVAL_SEC = 0.2

# How long to wait before retrying after a connection error in the subscribe loop.
_RECONNECT_DELAY_SEC = 3.0


def run_subscribe_loop(
    reader: RedisQuotesReader,
    on_quote: Callable[[Dict[str, Any]], None],
    stop_event: threading.Event,
    poll_timeout: float = 0.5,
) -> None:
    """Run Redis SUBSCRIBE in a thread; on each message load full quote and call on_quote.

    Subscribes to ``subscribe_channel`` (default ``ib:ingester:channel``). Expects IB ingestor
    publish payload ``{contract_key, ts}`` and loads ``ib:ingester:tick:{contract_key}``.

    Resilience:
    - ``socket_timeout`` on the sub_client prevents blocking forever on stale TCP connections.
    - On any Redis / connection error the loop logs, waits, and reconnects.
    - Per-symbol throttle avoids hundreds of redundant Redis GETs when the ingestor publishes
      many ticks for the same symbol within a short window.
    """
    try:
        import redis as redis_pkg
    except ImportError:
        logger.warning("redis package not installed; SSE quote stream disabled")
        return

    p = reader.realtime_params
    # last_broadcast_ts[contract_key] = epoch time of most recent broadcast
    last_broadcast_ts: Dict[str, float] = {}

    while not stop_event.is_set():
        sub_client = None
        pubsub = None
        try:
            sub_client = redis_pkg.Redis(
                host=p.host,
                port=p.port,
                db=p.db,
                password=p.password if p.password else None,
                socket_connect_timeout=p.socket_connect_timeout,
                socket_timeout=p.socket_connect_timeout,
                decode_responses=True,
            )
            pubsub = sub_client.pubsub()
            pubsub.subscribe(p.subscribe_channel)
            logger.info(
                "Redis quotes subscribe loop started on channel=%s",
                p.subscribe_channel,
            )

            while not stop_event.is_set():
                msg = pubsub.get_message(timeout=poll_timeout)
                if msg is None:
                    continue
                if msg.get("type") != "message":
                    continue
                try:
                    data = json.loads(msg["data"])
                    contract_key = (data.get("contract_key") or "").strip()
                    if not contract_key:
                        sym = (data.get("symbol") or "").strip()
                        if sym:
                            contract_key = f"{sym}|STK|||"
                    if not contract_key:
                        continue

                    # Throttle: skip if we broadcast this symbol too recently
                    now = time.monotonic()
                    if now - last_broadcast_ts.get(contract_key, 0.0) < _MIN_BROADCAST_INTERVAL_SEC:
                        continue

                    quote = reader.get_ingester_tick(contract_key)
                    if quote:
                        last_broadcast_ts[contract_key] = now
                        on_quote(quote)
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug("Redis subscribe message parse skip: %s", e)
                except Exception as e:
                    logger.warning("Redis subscribe inner error: %s", e)
                    break  # reconnect

        except Exception as e:
            logger.warning("Redis subscribe loop connection error: %s — reconnecting in %.0fs", e, _RECONNECT_DELAY_SEC)
        finally:
            if pubsub is not None:
                try:
                    pubsub.close()
                except Exception:
                    pass
            if sub_client is not None:
                try:
                    sub_client.close()
                except Exception:
                    pass

        if not stop_event.is_set():
            stop_event.wait(timeout=_RECONNECT_DELAY_SEC)

    logger.info("Redis quotes subscribe loop stopped")
