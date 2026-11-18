import asyncio
import json

import httpx
import pytest
from conftest import payload, populate

from cahuita_conditions.models import Source


async def test_cold_start_api_page_and_docs(app_factory):
    async with app_factory() as (app, client):
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready")).status_code == 503
        response = await client.get("/api/v1/locations/cahuita/forecast")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "forecast_unavailable"
        assert (await client.get("/")).status_code == 200
        assert "Forecast unavailable" in (await client.get("/")).text
        assert (await client.get("/api/v1/locations")).json()[0]["id"] == "cahuita"
        assert (await client.get("/api/v1/locations/unknown/forecast")).status_code == 404
        assert (await client.get("/?location=unknown")).status_code == 404
        schema = (await client.get("/openapi.json")).json()
        assert "ForecastResponse" in schema["components"]["schemas"]
        assert "/health/ready" in schema["paths"]


async def test_fresh_forecast_and_no_request_driven_provider_calls(app_factory, clock):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        source = "marine" if request.url.path.endswith("marine") else "weather"
        return httpx.Response(200, json=payload(source, clock()))

    async with app_factory(handler) as (app, client):
        await populate(app)
        assert calls == 2
        result = (await client.get("/api/v1/locations/cahuita/forecast")).json()
        assert result["status"] == "ok"
        assert len(result["weather"]["hourly"]) == 48
        assert result["weather"]["hourly"][0]["timestamp"].endswith("Z")
        assert result["weather"]["units"]["temperature_2m"] == "°C"
        assert (await client.get("/health/ready")).status_code == 200
        page = await client.get("/")
        assert "2026-11-02 06:00" in page.text
        assert "<script" not in page.text
        assert 'method="get"' in page.text
        assert "table-condensed table-bordered" in page.text
        assert "btn btn-default btn-sm" in page.text
        assert (await client.get("/api/v1/status")).json()["ready"]
        await client.get("/metrics")
        await client.get("/api/v1/locations/cahuita/forecast")
        assert calls == 2


async def test_fresh_stale_expired_and_dynamic_metrics(app_factory, clock):
    async with app_factory() as (app, client):
        await populate(app)
        clock.advance(1800)
        app.state.service.last_tick = clock()
        result = (await client.get("/api/v1/locations/cahuita/forecast")).json()
        assert result["weather"]["freshness"] == "stale"
        assert (await client.get("/health/ready")).status_code == 200
        metrics = (await client.get("/metrics")).text
        assert (
            'forecast_cache_age_seconds{location_id="cahuita",source="weather"} 1800.0' in metrics
        )
        clock.advance(21600 - 1800)
        app.state.service.last_tick = clock()
        result = await client.get("/api/v1/locations/cahuita/forecast")
        assert result.status_code == 503
        assert result.json()["weather"]["reason"] == "expired"
        assert result.json()["weather"]["hourly"] == []
        assert (await client.get("/health/ready")).status_code == 503
        assert (await client.get("/health/live")).status_code == 200
        assert "No usable forecast records" in (await client.get("/")).text
        assert (
            'forecast_data_usable{location_id="cahuita",source="weather"} 0.0'
            in (await client.get("/metrics")).text
        )


async def test_one_source_expires_other_recovers(app_factory, clock):
    modes = {"weather": "ok", "marine": "ok"}

    def handler(request):
        source = "marine" if request.url.path.endswith("marine") else "weather"
        if modes[source] == "error":
            return httpx.Response(503)
        return httpx.Response(200, json=payload(source, clock()))

    async with app_factory(handler) as (app, client):
        await populate(app)
        modes["marine"] = "error"
        clock.advance(21600)
        await populate(app)
        app.state.service.last_tick = clock()
        response = await client.get("/api/v1/locations/cahuita/forecast")
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "degraded"
        assert result["weather"]["freshness"] == "fresh"
        assert result["marine"]["freshness"] == "unavailable"
        assert (await client.get("/health/ready")).status_code == 200
        modes["marine"] = "ok"
        await populate(app)
        assert (await client.get("/api/v1/locations/cahuita/forecast")).json()["status"] == "ok"


@pytest.mark.parametrize("mode", ["malformed", "empty", "mismatch", "nulls"])
async def test_bad_update_preserves_last_valid_snapshot(app_factory, clock, mode):
    bad = False

    def handler(request):
        source = "marine" if request.url.path.endswith("marine") else "weather"
        if bad and mode == "malformed":
            return httpx.Response(200, text="not JSON")
        data = payload(source, clock())
        if bad:
            if mode == "empty":
                data["hourly"]["time"] = []
            elif mode == "mismatch":
                data["hourly"][next(iter(data["hourly"].keys() - {"time"}))] = []
            else:
                for field in data["hourly"].keys() - {"time"}:
                    data["hourly"][field] = [None] * 72
        return httpx.Response(200, json=data)

    async with app_factory(handler) as (app, client):
        await populate(app)
        fetched = app.state.service.states[("cahuita", Source.WEATHER)].snapshot.fetched_at
        bad = True
        clock.advance(60)
        await populate(app)
        result = (await client.get("/api/v1/locations/cahuita/forecast")).json()
        assert result["status"] == "degraded"
        assert result["weather"]["last_outcome"] == "invalid_payload"
        assert result["weather"]["last_success_at"] == fetched.isoformat().replace("+00:00", "Z")
        assert result["weather"]["available_hours"] == 48


async def test_partial_values_and_gaps_are_not_interpolated(app_factory, clock):
    def handler(request):
        source = "marine" if request.url.path.endswith("marine") else "weather"
        data = payload(source, clock())
        for field in data["hourly"].keys() - {"time"}:
            data["hourly"][field][0] = None
        return httpx.Response(200, json=data)

    async with app_factory(handler) as (app, client):
        await populate(app)
        result = (await client.get("/api/v1/locations/cahuita/forecast")).json()
        assert result["status"] == "degraded"
        assert result["weather"]["available_hours"] == 47
        assert result["weather"]["hourly"][0]["values"]["temperature_2m"] is None


async def test_hourly_coverage_runs_out_before_cache_age_limit(app_factory, clock):
    def handler(request):
        source = "marine" if request.url.path.endswith("marine") else "weather"
        return httpx.Response(200, json=payload(source, clock(), hours=1))

    async with app_factory(handler) as (app, client):
        await populate(app)
        clock.advance(3600)
        result = (await client.get("/api/v1/locations/cahuita/forecast")).json()
        assert result["status"] == "unavailable"
        assert result["weather"]["reason"] == "no_upcoming_forecast"


async def test_storage_restart_and_configuration_invalidation(app_factory, clock, tmp_path):
    async with app_factory() as (app, client):
        await populate(app)
        original = (await client.get("/api/v1/locations/cahuita/forecast")).json()
    clock.advance(60)
    async with app_factory(lambda request: httpx.Response(503)) as (app, client):
        restored = (await client.get("/api/v1/locations/cahuita/forecast")).json()
        assert restored["weather"]["last_success_at"] == original["weather"]["last_success_at"]
        assert (await client.get("/health/ready")).status_code == 200
    location_file = tmp_path / "changed.json"
    location_file.write_text(
        json.dumps(
            [
                {
                    "id": "cahuita",
                    "name": "Cahuita",
                    "latitude": 10,
                    "longitude": -82.84,
                    "timezone": "America/Costa_Rica",
                }
            ]
        )
    )
    async with app_factory(locations_file=location_file) as (app, client):
        assert (await client.get("/api/v1/locations/cahuita/forecast")).status_code == 503


async def test_readiness_detects_dead_loop_and_storage(app_factory):
    async with app_factory() as (app, client):
        await populate(app)
        app.state.service.task.cancel()
        await asyncio.gather(app.state.service.task, return_exceptions=True)
        result = await client.get("/health/ready")
        assert result.status_code == 503
        assert "refresh_loop_unhealthy" in result.json()["reasons"]
        assert (await client.get("/health/live")).status_code == 200
    async with app_factory() as (app, client):
        await populate(app)
        await app.state.service.store.close()
        result = await client.get("/health/ready")
        assert result.status_code == 503
        assert "storage_unhealthy" in result.json()["reasons"]


async def test_missing_snapshot_metrics_and_bounded_routes(app_factory):
    async with app_factory() as (app, client):
        await client.get("/no-one")
        await client.get("/no-two")
        await client.get("/api/v1/locations/not-configured/forecast")
        for _ in range(2):
            await client.get("/metrics")
        result = await client.get("/metrics")
        assert result.headers["content-type"].startswith("text/plain")
        metrics = result.text
        assert 'route="__unmatched__"' in metrics
        assert 'route="/api/v1/locations/{location_id}/forecast"' in metrics
        assert 'route="/metrics"' not in metrics
        assert 'route="/no-one"' not in metrics
        assert "forecast_cache_age_seconds{" not in metrics
        assert "app_build_info{" in metrics


async def test_scheduler_honors_backoff_without_blocking_other_sources(app_factory, clock):
    calls = {"weather": 0, "marine": 0}

    def handler(request):
        source = "marine" if request.url.path.endswith("marine") else "weather"
        calls[source] += 1
        if source == "weather":
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, json=payload(source, clock()))

    async with app_factory(
        handler,
        auto_refresh=True,
        heartbeat_seconds=0.01,
        refresh_seconds=0.1,
        stale_seconds=1,
        expire_seconds=2,
    ) as (app, client):
        await asyncio.gather(*app.state.service.in_flight.values())
        assert calls == {"weather": 1, "marine": 1}
        clock.advance(1)
        await asyncio.sleep(0.04)
        assert calls["weather"] == 1
        assert calls["marine"] == 2
        clock.advance(120)
        await asyncio.sleep(0.04)
        assert calls["weather"] == 2


async def test_storage_write_failure_preserves_snapshot(app_factory, clock, monkeypatch):
    async with app_factory() as (app, client):
        await populate(app)
        original = app.state.service.states[("cahuita", Source.WEATHER)].snapshot

        async def fail_save(*args):
            raise OSError("Disk full")

        monkeypatch.setattr(app.state.service.store, "save", fail_save)
        clock.advance(60)
        await app.state.service.refresh(app.state.service.locations[0], Source.WEATHER)
        assert app.state.service.states[("cahuita", Source.WEATHER)].snapshot == original
        assert not (await app.state.service.status()).storage_healthy
        assert (await client.get("/health/ready")).status_code == 503


async def test_scheduler_heartbeat_detects_a_stalled_loop(app_factory, clock):
    async with app_factory() as (app, client):
        await populate(app)
        clock.advance(6)
        assert not (await app.state.service.status()).refresh_loop_healthy
