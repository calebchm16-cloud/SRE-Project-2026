"""Per-application metric registries and tracer providers, safe for isolated tests."""

import contextvars
import json
import logging
import sys
import time
import uuid
from datetime import UTC, datetime

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    Info,
    PlatformCollector,
    ProcessCollector,
)
from starlette.datastructures import MutableHeaders

from . import __version__

request_id = contextvars.ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        context = trace.get_current_span().get_span_context()
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
            "request_id": request_id.get(),
            "trace_id": f"{context.trace_id:032x}" if context.is_valid else None,
            "span_id": f"{context.span_id:016x}" if context.is_valid else None,
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    root = logging.getLogger()
    # Preserve caller/test handlers; install our handler once across app lifespans.
    if not any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.cahuita_json = True
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def make_tracer_provider(settings, exporter=None):
    if not settings.otlp_traces_endpoint and exporter is None:
        return trace.NoOpTracerProvider()
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": "cahuita-conditions", "service.version": __version__}
        ),
        sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
        shutdown_on_exit=False,
    )
    exporter = exporter or OTLPSpanExporter(endpoint=settings.otlp_traces_endpoint, timeout=1)
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            max_queue_size=256,
            max_export_batch_size=32,
            schedule_delay_millis=1000,
            export_timeout_millis=1000,
        )
    )
    return provider


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)
        GCCollector(registry=self.registry)
        self.http = Counter(
            "http_requests_total",
            "HTTP responses",
            ["method", "route", "status_code"],
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "http_request_duration_seconds",
            "HTTP latency",
            ["method", "route"],
            registry=self.registry,
        )
        self.upstream = Counter(
            "upstream_requests_total",
            "Provider attempts",
            ["source", "outcome"],
            registry=self.registry,
        )
        self.upstream_duration = Histogram(
            "upstream_request_duration_seconds",
            "Provider attempt latency",
            ["source"],
            registry=self.registry,
        )
        self.refresh = Counter(
            "forecast_refresh_total",
            "Refresh operations",
            ["location_id", "source", "outcome"],
            registry=self.registry,
        )
        labels = ["location_id", "source"]
        self.last_success = Gauge(
            "forecast_last_success_timestamp_seconds",
            "Last validated fetch, Unix time",
            labels,
            registry=self.registry,
        )
        self.age = Gauge(
            "forecast_cache_age_seconds",
            "Age since last validated fetch",
            labels,
            registry=self.registry,
        )
        self.hours = Gauge(
            "forecast_available_hours",
            "Usable forecast hours in the next 48 hours",
            labels,
            registry=self.registry,
        )
        self.usable = Gauge(
            "forecast_data_usable", "One when a section is usable", labels, registry=self.registry
        )
        self.tick = Gauge(
            "refresh_loop_last_tick_timestamp_seconds",
            "Refresh scheduler heartbeat",
            registry=self.registry,
        )
        Info("app_build", "Application build", registry=self.registry).info(
            {"version": __version__}
        )


class RequestTelemetry:
    def __init__(self, app, metrics: Metrics):
        self.app, self.metrics = app, metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/metrics":
            return await self.app(scope, receive, send)
        token = request_id.set(uuid.uuid4().hex)
        start, status = time.perf_counter(), 500

        async def wrapped_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["x-request-id"] = request_id.get()
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        finally:
            duration = time.perf_counter() - start
            route = getattr(scope.get("route"), "path", "__unmatched__")
            method = (
                scope["method"]
                if scope["method"] in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
                else "OTHER"
            )
            self.metrics.http.labels(method, route, str(status)).inc()
            self.metrics.http_duration.labels(method, route).observe(duration)
            logging.getLogger("cahuita.http").info(
                "http_request",
                extra={
                    "fields": {
                        "method": method,
                        "route": route,
                        "status_code": status,
                        "duration_seconds": duration,
                    }
                },
            )
            request_id.reset(token)
