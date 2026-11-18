import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cahuita_conditions.app import create_app
from cahuita_conditions.config import Settings
from cahuita_conditions.models import Source

SAMPLES = json.loads(Path(__file__).with_name("fixtures.json").read_text())


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 2, 12, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def payload(source, now, hours=72):
    sample = SAMPLES[source]
    floor = now.replace(minute=0, second=0, microsecond=0)
    return {
        "latitude": 9.791664,
        "longitude": -82.70833,
        "hourly_units": {"time": "unixtime", **sample["units"]},
        "hourly": {
            "time": [int((floor + timedelta(hours=i)).timestamp()) for i in range(hours)],
            **{field: [value] * hours for field, value in sample["values"].items()},
        },
    }


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app_factory(tmp_path, clock):
    @asynccontextmanager
    async def factory(handler=None, *, auto_refresh=False, exporter=None, **overrides):
        def default(request):
            source = "marine" if request.url.path.endswith("marine") else "weather"
            return httpx.Response(200, json=payload(source, clock()))

        settings = Settings(
            _env_file=None,
            database_path=tmp_path / "cache.sqlite3",
            retry_delay_seconds=0,
            **overrides,
        )
        app = create_app(
            settings,
            clock=clock,
            transport=httpx.MockTransport(handler or default),
            auto_refresh=auto_refresh,
            exporter=exporter,
        )
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield app, client

    return factory


async def populate(app):
    service = app.state.service
    await asyncio.gather(
        *(service.refresh(location, source) for location in service.locations for source in Source)
    )
