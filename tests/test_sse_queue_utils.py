"""Tests for servers.sse_queue_utils."""

import asyncio

import pytest

from bifrost_core.sse.queue_utils import put_nowait_drop_oldest


@pytest.mark.asyncio
async def test_put_nowait_drop_oldest_prefers_latest_when_full() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    put_nowait_drop_oldest(q, 1)
    put_nowait_drop_oldest(q, 2)
    put_nowait_drop_oldest(q, 3)
    assert await q.get() == 2
    assert await q.get() == 3


def test_put_nowait_drop_oldest_sync_bounded() -> None:
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    put_nowait_drop_oldest(q, "a")
    put_nowait_drop_oldest(q, "b")
    assert q.get_nowait() == "b"
