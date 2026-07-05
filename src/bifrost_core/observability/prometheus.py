"""Prometheus metrics helpers for FastAPI services."""

from __future__ import annotations

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator


def instrument_app(app: FastAPI, service_name: str) -> None:
    """Attach request metrics middleware and expose ``GET /metrics``.

    ``service_name`` identifies the API domain (e.g. ``api-monitor``) for callers;
    Kubernetes scrape labels provide per-service series in Prometheus.
    """
    _ = service_name
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_respect_env_var=True,
        should_instrument_requests_inprogress=True,
        excluded_handlers={"/metrics"},
        inprogress_name="http_requests_inprogress",
        inprogress_labels=True,
    ).instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
    )
