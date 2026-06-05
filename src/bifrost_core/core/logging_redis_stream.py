"""Push log lines to a Redis Stream (Monitor UI console pattern)."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

try:
    import redis
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover
    redis = None  # type: ignore[misc, assignment]
    RedisError = Exception  # type: ignore[misc, assignment]


class RedisStreamLogHandler(logging.Handler):
    """Append formatted log records to a Redis stream (XADD, optional MAXLEN)."""

    def __init__(self, redis_url: str, stream_key: str, maxlen: int = 500) -> None:
        super().__init__()
        self._redis_url = redis_url
        self._stream_key = stream_key
        self._maxlen = maxlen
        self._logged_emit_error = False

    def emit(self, record: logging.LogRecord) -> None:
        if redis is None:
            return
        try:
            line = self.format(record)
            r = redis.from_url(self._redis_url)
            r.xadd(
                self._stream_key,
                {"line": line},
                maxlen=self._maxlen,
                approximate=True,
            )
        except RedisError as e:
            if not self._logged_emit_error:
                self._logged_emit_error = True
                print(
                    f"RedisStreamLogHandler: first xadd failed for {self._stream_key!r} ({e!r}); "
                    "check redis host/port/password vs merged YAML, and that Monitor uses the same Redis.",
                    file=sys.stderr,
                )
