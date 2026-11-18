import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from conftest import payload

from cahuita_conditions.config import Settings, load_locations
from cahuita_conditions.models import Location, Source
from cahuita_conditions.provider import ProviderError, normalize, retry_after


@pytest.mark.parametrize(
    "override",
    [
        {"refresh_seconds": 1800},
        {"stale_seconds": 21600},
        {"trace_sample_ratio": 2},
        {"weather_base_url": "ftp://example.org"},
        {"marine_base_url": "https://user:pass@example.org"},
        {"log_level": "what"},
    ],
)
def test_invalid_settings(override):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **override)


def test_locations_validation(tmp_path):
    path = tmp_path / "locations.json"
    location = {"id": "one", "name": "One", "latitude": 9, "longitude": -82, "timezone": "UTC"}
    for data in [
        [],
        [location, location],
        [{**location, "timezone": "Unknown/Zone"}],
        [{**location, "latitude": 91}],
        [{**location, "id": "User Provided / Path"}],
    ]:
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            load_locations(path)
    path.write_text(json.dumps([location]))
    assert load_locations(path)[0].id == "one"


@pytest.mark.parametrize("source", list(Source))
def test_normalization_units_utc_and_nulls(source, clock):
    data = payload(source, clock())
    field = next(iter(data["hourly_units"].keys() - {"time"}))
    data["hourly"][field][1] = None
    snapshot = normalize(data, source, clock())
    assert snapshot.fetched_at == clock()
    assert snapshot.hourly[0].timestamp.isoformat() == "2026-11-02T12:00:00+00:00"
    assert snapshot.hourly[1].values[field] is None
    assert snapshot.grid.longitude == -82.70833


@pytest.mark.parametrize(
    "corruption",
    [
        "empty",
        "mismatch",
        "unit",
        "time_unit",
        "unsorted",
        "non_hour",
        "old",
        "nulls",
        "nan",
        "bool",
        "probability",
        "direction",
        "negative",
        "grid",
    ],
)
def test_invalid_payloads(corruption, clock):
    data = payload("weather", clock())
    hourly = data["hourly"]
    if corruption == "empty":
        hourly["time"] = []
    elif corruption == "mismatch":
        hourly["precipitation"] = [1]
    elif corruption == "unit":
        data["hourly_units"]["wind_speed_10m"] = "mph"
    elif corruption == "time_unit":
        data["hourly_units"]["time"] = "iso8601"
    elif corruption == "unsorted":
        hourly["time"][1] = hourly["time"][0]
    elif corruption == "non_hour":
        hourly["time"][0] += 60
    elif corruption == "old":
        hourly["time"] = [stamp - 86400 * 5 for stamp in hourly["time"]]
    elif corruption == "nulls":
        for field in hourly.keys() - {"time"}:
            hourly[field] = [None] * 72
    elif corruption == "nan":
        hourly["temperature_2m"][0] = float("nan")
    elif corruption == "bool":
        hourly["temperature_2m"][0] = True
    elif corruption == "probability":
        hourly["precipitation_probability"][0] = 101
    elif corruption == "direction":
        hourly["wind_direction_10m"][0] = 361
    elif corruption == "negative":
        hourly["wind_speed_10m"][0] = -1
    elif corruption == "grid":
        data["latitude"] = 91
    with pytest.raises(ValueError):
        normalize(data, Source.WEATHER, clock())


@pytest.mark.parametrize(("status", "attempts"), [(400, 1), (401, 1), (503, 2), (429, 1)])
async def test_provider_http_retry_policy(app_factory, status, attempts, clock):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "45"})

    async with app_factory(handler) as (app, client):
        service = app.state.service
        with pytest.raises(ProviderError) as error:
            await service.provider.fetch(service.locations[0], Source.WEATHER)
        assert len(calls) == attempts
        if status == 429:
            assert error.value.retry_at == clock() + timedelta(seconds=45)


async def test_retry_then_success_and_request_contract(app_factory, clock):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("offline")
        return httpx.Response(200, json=payload("marine", clock()))

    async with app_factory(handler) as (app, client):
        snapshot = await app.state.service.provider.fetch(
            app.state.service.locations[0], Source.MARINE
        )
        assert snapshot.hourly
    assert len(calls) == 2
    assert calls[-1].url.path == "/v1/marine"
    assert calls[-1].url.params["cell_selection"] == "sea"
    assert calls[-1].url.params["timeformat"] == "unixtime"
    assert calls[-1].url.params["timezone"] == "UTC"
    assert calls[-1].url.params["forecast_days"] == "3"


async def test_total_deadline_and_independent_source(app_factory, clock):
    async def handler(request):
        if request.url.path.endswith("forecast"):
            await asyncio.sleep(0.1)
        return httpx.Response(200, json=payload("marine", clock()))

    async with app_factory(handler, request_timeout_seconds=0.01) as (app, client):
        service = app.state.service
        await asyncio.gather(*(service.refresh(service.locations[0], source) for source in Source))
        result = (await client.get("/api/v1/locations/cahuita/forecast")).json()
        assert result["weather"]["last_error"] == "timeout"
        assert result["marine"]["freshness"] == "fresh"
        assert (await client.get("/health/ready")).status_code == 200


def test_retry_after_formats(clock):
    now = clock()
    assert retry_after("120", now, 900) == now + timedelta(seconds=120)
    assert retry_after("Mon, 02 Nov 2026 12:02:00 GMT", now, 900) == now + timedelta(seconds=120)
    for invalid in [None, "garbage", "-1", "nan", "inf"]:
        assert retry_after(invalid, now, 900) == now + timedelta(seconds=900)


async def test_cancelled_upstream_is_not_counted_as_success(app_factory):
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def handler(request):
        entered.set()
        await blocked.wait()

    async with app_factory(handler) as (app, client):
        service = app.state.service
        task = asyncio.create_task(service.provider.fetch(service.locations[0], Source.WEATHER))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        metrics = (await client.get("/metrics")).text
        assert 'upstream_requests_total{outcome="cancelled",source="weather"} 1.0' in metrics
        assert 'upstream_requests_total{outcome="success"' not in metrics


async def test_max_four_provider_calls(app_factory, clock, tmp_path):
    locations_file = tmp_path / "multi.json"
    locations_file.write_text(
        json.dumps(
            [
                Location(
                    id=f"location-{i}", name="Test", latitude=9, longitude=-82, timezone="UTC"
                ).model_dump()
                for i in range(3)
            ]
        )
    )
    active = peak = calls = 0

    async def handler(request):
        nonlocal active, peak, calls
        active += 1
        calls += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        source = "marine" if request.url.path.endswith("marine") else "weather"
        return httpx.Response(200, json=payload(source, clock()))

    async with app_factory(handler, locations_file=locations_file, auto_refresh=True) as (
        app,
        client,
    ):
        async with asyncio.timeout(3):
            await asyncio.gather(*app.state.service.in_flight.values())
        assert calls == 6
        assert peak == 4
