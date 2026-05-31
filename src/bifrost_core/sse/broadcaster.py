"""Thread-safe SSE broadcaster for FastAPI streaming endpoints.

Pattern used by bifrost_api (market, massive, monitor domains):

    # In FastAPI lifespan / app startup:
    broadcaster = SseBroadcaster(maxsize=256)
    app.state.quotes_broadcaster = broadcaster

    # Background thread (Redis pub/sub consumer):
    broadcaster.broadcast({"symbol": "NVDA", "bid": 123.4})

    # SSE endpoint:
    async def stream(request: Request):
        q = app.state.quotes_broadcaster.subscribe()
        try:
            async def _gen():
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        payload = await asyncio.wait_for(q.get(), timeout=25.0)
                        yield f"data: {json.dumps(payload)}\\n\\n"
                    except asyncio.TimeoutError:
                        yield ": heartbeat\\n\\n"
            return StreamingResponse(_gen(), media_type="text/event-stream")
        finally:
            app.state.quotes_broadcaster.unsubscribe(q)

Replaces the per-app duplicate pattern of ``app.state.*_sse_queues + Lock +
put_nowait_drop_oldest`` that existed in legacy bifrost-trader-engine.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, List, Optional


class SseBroadcaster:
    """Fan-out broadcaster: one producer thread → many asyncio consumer queues.

    Thread-safe: ``broadcast()`` may be called from any thread.  Consumer
    ``subscribe`` / ``unsubscribe`` must be called from the asyncio event loop
    (they are not thread-safe — FastAPI endpoints run in the loop, so this is fine).
    """

    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize = maxsize
        self._queues: List[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── consumer API (call from asyncio loop) ─────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        """Register a new consumer and return its queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        with self._lock:
            self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Deregister a consumer queue."""
        with self._lock:
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind to an event loop for thread-safe ``broadcast()`` calls.

        Call once during FastAPI lifespan startup::

            broadcaster.attach_loop(asyncio.get_event_loop())
        """
        self._loop = loop

    # ── producer API (thread-safe) ────────────────────────────────────────────

    def broadcast(self, payload: Any) -> None:
        """Push *payload* to all subscribed queues from any thread.

        Full queues silently drop their oldest entry to make room.
        Requires ``attach_loop()`` to have been called first.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(self._broadcast_in_loop, payload)

    # ── internal ──────────────────────────────────────────────────────────────

    def _broadcast_in_loop(self, payload: Any) -> None:
        """Must run inside the asyncio event loop (called via call_soon_threadsafe)."""
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            _put_nowait_drop_oldest(q, payload)


# ── helpers ───────────────────────────────────────────────────────────────────

def _put_nowait_drop_oldest(q: asyncio.Queue, item: Any) -> None:
    """Enqueue without blocking; drop oldest entries if full."""
    while True:
        try:
            q.put_nowait(item)
            return
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
