import asyncio
import io
import json
import logging

import pytest
from conftest import populate
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from cahuita_conditions.storage import CacheStore
from cahuita_conditions.telemetry import JsonFormatter


async def test_trace_parentage_and_log_correlation(app_factory):
    exporter = InMemorySpanExporter()
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("cahuita")
    logger.addHandler(handler)
    try:
        async with app_factory(exporter=exporter) as (app, client):
            await populate(app)
            reply = await client.get(
                "/api/v1/locations/cahuita/forecast",
                headers={
                    "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
                },
            )
            await client.get("/health/live")
            await client.get("/metrics")
            await asyncio.to_thread(app.state.tracing.force_flush)
            spans = exporter.get_finished_spans()
            server = next(span for span in spans if span.name.startswith("GET /api/v1/locations"))
            assert f"{server.context.trace_id:032x}" == "11111111111111111111111111111111"
            refreshes = [span for span in spans if span.name == "forecast.refresh"]
            assert len(refreshes) == 2
            assert any(
                span.parent and span.parent.span_id == refreshes[0].context.span_id
                for span in spans
            )
            assert not any("/health/" in span.name or "/metrics" in span.name for span in spans)
            logs = [json.loads(line) for line in output.getvalue().splitlines()]
            request = next(
                row
                for row in logs
                if row["event"] == "http_request"
                and row.get("route") == "/api/v1/locations/{location_id}/forecast"
            )
            assert request["trace_id"] == "11111111111111111111111111111111"
            assert request["request_id"] == reply.headers["x-request-id"]
    finally:
        logger.removeHandler(handler)


async def test_unreachable_exporter_does_not_affect_readiness(app_factory):
    async with app_factory(otlp_traces_endpoint="http://127.0.0.1:1/v1/traces") as (app, client):
        await populate(app)
        await client.get("/")
        await asyncio.to_thread(app.state.tracing.force_flush)
        assert (await client.get("/health/ready")).status_code == 200
        assert (await client.get("/api/v1/locations/cahuita/forecast")).status_code == 200


async def test_storage_initialization_error_is_actionable(tmp_path):
    file = tmp_path / "not-a-directory"
    file.write_text("x")
    store = CacheStore(file / "cache.sqlite3")
    with pytest.raises(RuntimeError, match="Cannot initialize SQLite cache"):
        await store.open()


async def test_corrupt_database_is_not_deleted(tmp_path):
    file = tmp_path / "broken.sqlite3"
    file.write_bytes(b"corrupt cache")
    with pytest.raises(RuntimeError, match="Cannot initialize SQLite cache"):
        await CacheStore(file).open()
    assert file.read_bytes() == b"corrupt cache"
