"""Prometheus metrics helpers for FastAPI services.

Series (same names and labels as prometheus-fastapi-instrumentator's default set):

- ``http_requests_total{method, status, handler}`` — every request but ``/metrics``.
- ``http_request_size_bytes{handler}`` / ``http_response_size_bytes{handler}`` — Content-Length.
- ``http_request_duration_seconds{method, handler}`` — buckets 0.1 / 0.5 / 1 (per handler).
- ``http_request_duration_highr_seconds`` — no labels, buckets 0.01 … 60 s. The alert rule
  ``BifrostAPIHighLatency`` (bifrost-trade-infra ``k8s/monitoring/bifrost-alerting-rules.yaml``)
  reads this one: the per-handler histogram tops out at 1 s, so a p99 read from it never
  exceeds 1 s (TD-194).

Two things differ from the instrumentator's default, so the latency histograms measure
request handling and nothing else (TD-194; the research / plugin middlewares and platform-api
already measure this way, TD-161 / TD-195):

- **Latency is time to the response headers.** An SSE stream (``/quotes/stream``,
  ``/api/messages/stream``) used to be timed to the end of the connection — tens of seconds
  per client — which on PROD api-monitor was the whole > 2 s tail. A response whose headers
  were never sent (an exception escaped the app) keeps the full duration.
- **Health probes are counted, not timed.** kubelet hits ``/health`` every few seconds; over
  the 7 days to 2026-10-06 it was 87–97 % of each Trade API's latency observations, so a p99
  over all of them was roughly the p70 of real requests.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from dataclasses import dataclass

from prometheus_client import REGISTRY, CollectorRegistry, Gauge
from prometheus_fastapi_instrumentator import Instrumentator, metrics
from prometheus_fastapi_instrumentator.middleware import PrometheusInstrumentatorMiddleware
from starlette.applications import Starlette
from starlette.types import ASGIApp

#: Never recorded at all (the instrumentator's ``excluded_handlers`` are regexes; anchored).
UNRECORDED_HANDLERS = frozenset({"/metrics"})
#: Counted in ``http_requests_total``, kept out of both latency histograms.
UNTIMED_HANDLERS = frozenset({"/health"})

INPROGRESS_NAME = "http_requests_inprogress"
LOWR_BUCKETS = (0.1, 0.5, 1)
HIGHR_BUCKETS = (
    0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 2.5, 3,
    3.5, 4, 4.5, 5, 7.5, 10, 30, 60,
)  # fmt: skip


def _timed(
    observe: Callable[[metrics.Info], None] | None,
) -> Callable[[metrics.Info], None] | None:
    """Wrap a latency instrumentation: skip untimed handlers, time to the headers."""
    if observe is None:
        return None

    def instrumentation(info: metrics.Info) -> None:
        if info.modified_handler in UNTIMED_HANDLERS:
            return
        if info.modified_duration_without_streaming <= 0.0:
            # No http.response.start was sent (the app raised): the full duration is all there is.
            info.modified_duration_without_streaming = info.modified_duration
        observe(info)

    return instrumentation


@dataclass(frozen=True)
class _RequestMetrics:
    """One registry's request series: built once, shared by every app instrumented on it."""

    instrumentations: tuple[Callable[[metrics.Info], None], ...]
    inprogress: Gauge


#: Per registry. The instrumentator's metric helpers return ``None`` when their series is
#: already in the registry, so a second ``instrument_app`` on the same registry used to get
#: no instrumentations at all and record nothing but the in-progress gauge. bifrost-trade-api's
#: monitor process builds the docs app (whose routes it absorbs) before its own, so from core
#: 0.55.0 the never-served docs app held every series and api-monitor recorded none.
_SHARED: weakref.WeakKeyDictionary[CollectorRegistry, _RequestMetrics] = (
    weakref.WeakKeyDictionary()
)


def _build(registry: CollectorRegistry) -> list[object | None]:
    """Register the series; an entry is ``None`` where the registry already had it."""
    try:
        inprogress: Gauge | None = Gauge(
            INPROGRESS_NAME,
            "Number of HTTP requests in progress.",
            labelnames=("method", "handler"),
            multiprocess_mode="livesum",
            registry=registry,
        )
    except ValueError as exc:
        if "Duplicated time" not in str(exc):
            raise
        inprogress = None
    return [
        metrics.requests(should_include_handler=True, registry=registry),
        metrics.request_size(
            should_include_method=False, should_include_status=False, registry=registry
        ),
        metrics.response_size(
            should_include_method=False, should_include_status=False, registry=registry
        ),
        _timed(
            metrics.latency(
                metric_doc="Latency to the response headers by handler (no /health).",
                should_include_status=False,
                should_exclude_streaming_duration=True,
                buckets=LOWR_BUCKETS,
                registry=registry,
            )
        ),
        _timed(
            metrics.latency(
                metric_name="http_request_duration_highr_seconds",
                metric_doc="Latency to the response headers, many buckets, no labels (no /health).",
                should_include_handler=False,
                should_include_method=False,
                should_include_status=False,
                should_exclude_streaming_duration=True,
                buckets=HIGHR_BUCKETS,
                registry=registry,
            )
        ),
        inprogress,
    ]


def _request_metrics(registry: CollectorRegistry) -> _RequestMetrics:
    """The registry's request series, registering them on first use.

    Registered afresh when none of them is in the registry (the first app, or a test that
    emptied it); reused when all of them are there from an earlier ``instrument_app``.
    Anything else — some present, or present without having been registered here — means
    another library owns those names, and recording into ``None`` would be silent, so raise.
    """
    built = _build(registry)
    if all(b is not None for b in built):
        *instrumentations, inprogress = built
        shared = _RequestMetrics(tuple(instrumentations), inprogress)  # type: ignore[arg-type]
        _SHARED[registry] = shared
        return shared
    if all(b is None for b in built) and registry in _SHARED:
        return _SHARED[registry]
    raise RuntimeError(
        "instrument_app: the request series are already in this registry and were not "
        "registered by instrument_app; requests would go unrecorded"
    )


class _SharedGaugeMiddleware(PrometheusInstrumentatorMiddleware):
    """The instrumentator's middleware with the registry's in-progress gauge handed in.

    The stock middleware creates ``http_requests_inprogress`` on the process-wide registry
    when its app builds the middleware stack, so a second app to serve a request raised on
    the duplicate. Here every app shares the gauge registered by ``_request_metrics``.
    """

    def __init__(self, app: ASGIApp, *, inprogress: Gauge, **kwargs: object) -> None:
        super().__init__(app, should_instrument_requests_inprogress=False, **kwargs)  # type: ignore[arg-type]
        self.should_instrument_requests_inprogress = True
        self.inprogress = inprogress


def instrument_app(
    app: Starlette, service_name: str, *, registry: CollectorRegistry = REGISTRY
) -> None:
    """Attach request metrics middleware and expose ``GET /metrics``.

    ``service_name`` identifies the API domain (e.g. ``api-monitor``) for callers;
    Kubernetes scrape labels provide per-service series in Prometheus. ``registry`` is for
    tests; the services use the process-wide default. Any number of apps may be instrumented
    on one registry: they share its series and each records its own requests into them.
    """
    _ = service_name
    shared = _request_metrics(registry)
    app.add_middleware(
        _SharedGaugeMiddleware,
        inprogress=shared.inprogress,
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_respect_env_var=False,
        excluded_handlers=[f"^{h}$" for h in sorted(UNRECORDED_HANDLERS)],
        inprogress_name=INPROGRESS_NAME,
        inprogress_labels=True,
        instrumentations=list(shared.instrumentations),
        registry=registry,
    )
    Instrumentator(should_respect_env_var=False, registry=registry).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
    )
