"""Request metrics of the Trade APIs (TD-194): what the latency histograms measure.

``BifrostAPIHighLatency`` (bifrost-trade-infra) reads ``http_request_duration_highr_seconds``.
Before core 0.55.0 an SSE stream was timed to the end of the connection and kubelet's
``/health`` probes were most of the observations, so the p99 described neither real requests
nor their latency. Driven as a raw ASGI app (core has no httpx, so no TestClient).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from prometheus_client import REGISTRY, CollectorRegistry, Counter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from bifrost_core.observability.prometheus import HIGHR_BUCKETS, instrument_app

STREAM_SECONDS = 0.3


def _app(registry: CollectorRegistry) -> Starlette:
    """The api's apps are FastAPI (a Starlette subclass); core does not depend on fastapi."""

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def item(request: Request) -> JSONResponse:
        return JSONResponse({"id": request.path_params["item_id"]})

    async def slow(request: Request) -> JSONResponse:
        await asyncio.sleep(STREAM_SECONDS)
        return JSONResponse({"ok": True})

    async def stream(request: Request) -> StreamingResponse:
        async def gen():
            yield "data: first\n\n"
            await asyncio.sleep(STREAM_SECONDS)
            yield "data: second\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def boom(request: Request) -> JSONResponse:
        time.sleep(0.05)
        raise RuntimeError("boom")

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/items/{item_id}", item),
            Route("/slow", slow),
            Route("/stream", stream),
            Route("/boom", boom),
        ]
    )
    instrument_app(app, "api-test", registry=registry)
    return app


async def _get(app: Starlette, path: str) -> int:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }
    sent: list[dict] = []
    done = False

    async def receive() -> dict:
        nonlocal done
        if done:
            await asyncio.sleep(3600)
        done = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _v(registry: CollectorRegistry, name: str, **labels: str) -> float:
    return registry.get_sample_value(name, labels) or 0.0


@pytest.fixture
def registry():
    """A fresh registry per test; every series, the in-progress gauge too, lands in it."""
    before = set(REGISTRY._names_to_collectors)
    yield CollectorRegistry()
    assert set(REGISTRY._names_to_collectors) == before


async def test_health_is_counted_but_not_timed(registry):
    app = _app(registry)
    for _ in range(3):
        assert await _get(app, "/health") == 200
    assert await _get(app, "/items/7") == 200

    total = "http_requests_total"
    assert _v(registry, total, method="GET", status="2xx", handler="/health") == 3
    assert _v(registry, total, method="GET", status="2xx", handler="/items/{item_id}") == 1
    assert _v(registry, "http_request_duration_highr_seconds_count") == 1
    assert _v(registry, "http_request_duration_seconds_count", method="GET", handler="/health") == 0
    assert (
        _v(registry, "http_request_duration_seconds_count", method="GET", handler="/items/{item_id}")
        == 1
    )


async def test_metrics_endpoint_is_not_recorded(registry):
    app = _app(registry)
    assert await _get(app, "/metrics") == 200
    assert _v(registry, "http_requests_total", method="GET", status="2xx", handler="/metrics") == 0


async def test_a_stream_is_timed_to_its_headers_not_its_end(registry):
    app = _app(registry)
    t0 = time.perf_counter()
    assert await _get(app, "/stream") == 200
    assert time.perf_counter() - t0 >= STREAM_SECONDS

    assert _v(registry, "http_requests_total", method="GET", status="2xx", handler="/stream") == 1
    assert _v(registry, "http_request_duration_highr_seconds_count") == 1
    assert _v(registry, "http_request_duration_highr_seconds_sum") < STREAM_SECONDS / 3


async def test_a_slow_handler_is_still_timed_in_full(registry):
    app = _app(registry)
    assert await _get(app, "/slow") == 200
    assert _v(registry, "http_request_duration_highr_seconds_sum") >= STREAM_SECONDS
    # It lands above the 0.25 s bucket of the fine histogram and above the coarse 0.1 s one.
    assert _v(registry, "http_request_duration_highr_seconds_bucket", le="0.25") == 0
    assert _v(registry, "http_request_duration_seconds_bucket", method="GET", handler="/slow", le="0.1") == 0


async def test_an_exception_keeps_its_full_duration(registry):
    app = _app(registry)
    with pytest.raises(RuntimeError):
        await _get(app, "/boom")
    assert _v(registry, "http_requests_total", method="GET", status="5xx", handler="/boom") == 1
    assert _v(registry, "http_request_duration_highr_seconds_sum") >= 0.05


def test_the_fine_histogram_reaches_past_the_alert_threshold():
    # bifrost-trade-infra BifrostAPIHighLatency alerts on this histogram's p99 (TD-194);
    # scripts/check_http_metrics_coverage.py there asserts its threshold is below 60 s.
    assert max(HIGHR_BUCKETS) == 60
    assert list(HIGHR_BUCKETS) == sorted(HIGHR_BUCKETS)


# --- Several instrumented apps in one process (core 0.55.2) ---------------------------------
# bifrost-trade-api's monitor process builds the docs app (and instruments it) only to copy
# its routes, then instruments the monitor app. Under 0.55.0 / 0.55.1 the first call held
# every series and the second got none, so PROD api-monitor exported no http_requests_total.


async def test_the_second_app_on_a_registry_records_its_requests(registry):
    _app(registry)  # instrumented, never served: the docs app in the monitor process
    served = _app(registry)
    for _ in range(2):
        assert await _get(served, "/health") == 200
    assert await _get(served, "/items/7") == 200
    assert await _get(served, "/stream") == 200

    total = "http_requests_total"
    assert _v(registry, total, method="GET", status="2xx", handler="/health") == 2
    assert _v(registry, total, method="GET", status="2xx", handler="/items/{item_id}") == 1
    assert _v(registry, total, method="GET", status="2xx", handler="/stream") == 1
    assert _v(registry, "http_request_duration_highr_seconds_count") == 2
    assert _v(registry, "http_request_duration_highr_seconds_sum") < STREAM_SECONDS / 3
    assert _v(registry, "http_requests_inprogress", method="GET", handler="/items/{item_id}") == 0


async def test_two_served_apps_share_the_series_and_the_gauge(registry):
    first, second = _app(registry), _app(registry)
    assert await _get(first, "/items/1") == 200
    assert await _get(second, "/items/2") == 200
    assert await _get(first, "/health") == 200

    total = "http_requests_total"
    assert _v(registry, total, method="GET", status="2xx", handler="/items/{item_id}") == 2
    assert _v(registry, total, method="GET", status="2xx", handler="/health") == 1
    assert _v(registry, "http_requests_inprogress", method="GET", handler="/health") == 0


def test_series_owned_by_someone_else_fail_loudly(registry):
    Counter("http_requests_total", "Not ours.", ["method", "status", "handler"], registry=registry)
    with pytest.raises(RuntimeError, match="unrecorded"):
        _app(registry)
