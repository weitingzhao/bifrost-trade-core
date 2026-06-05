"""Helpers for asyncio.Queue used in SSE fan-out from background threads.

All producers that push into these queues must use ``put_nowait_drop_oldest`` so a
slow consumer cannot cause ``QueueFull`` in the event-loop callback.

Call sites:
- Market FastAPI app (``backend/market``): Redis quote broadcast → ``app.state.sse_queues`` (GET /quotes/stream)
- Monitor log SSE and similar: respective ``*_log_queues`` in ``backend/monitor``
"""

import asyncio
from typing import Any


def put_nowait_drop_oldest(q: asyncio.Queue, item: Any) -> None:
    """Enqueue for SSE consumers; never raise. If backlog, drop oldest entries until ``item`` fits.

    Prefer latest payload for quotes; for log lines, dropping oldest under backpressure is acceptable.
    """
    while True:
        try:
            q.put_nowait(item)
            return
        except asyncio.QueueFull:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
