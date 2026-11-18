"""Open-Meteo adapter. Normalize only validated hourly, metric-unit forecasts."""

import asyncio
import logging
import math
import random
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from .models import VARIABLES, Coordinates, HourlyPoint, Location, Snapshot, Source

log = logging.getLogger("cahuita.provider")


class ProviderError(Exception):
    def __init__(self, outcome: str, retry_at: datetime | None = None):
        super().__init__(outcome)
        self.outcome, self.retry_at = outcome, retry_at


def normalize(payload: dict, source: Source, now: datetime) -> Snapshot:
    """Reject structural problems; preserve genuine null values as visible gaps."""
    units = payload["hourly_units"]
    hourly = payload["hourly"]
    times = hourly["time"]
    if not isinstance(times, list) or not times or len(times) > 1000:
        raise ValueError("Invalid hourly time array")
    if units.get("time") != "unixtime":
        raise ValueError("Expected Unix timestamps")
    for field, unit in VARIABLES[source].items():
        if units.get(field) != unit:
            raise ValueError(f"Unexpected unit for {field}")
        if not isinstance(hourly[field], list) or len(hourly[field]) != len(times):
            raise ValueError("Mismatched hourly arrays")
    points = []
    previous = None
    for index, stamp in enumerate(times):
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp % 3600:
            raise ValueError("Expected UTC hour-aligned Unix timestamps")
        if previous is not None and stamp <= previous:
            raise ValueError("Times must be strictly increasing")
        previous = stamp
        values = {}
        for field in VARIABLES[source]:
            value = hourly[field][index]
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("Invalid numeric forecast value")
            if value is not None:
                if field == "precipitation_probability" and not 0 <= value <= 100:
                    raise ValueError("Invalid probability")
                if "direction" in field and not 0 <= value <= 360:
                    raise ValueError("Invalid direction")
                if field != "temperature_2m" and value < 0:
                    raise ValueError("Invalid negative forecast value")
            values[field] = value
        points.append(HourlyPoint(timestamp=datetime.fromtimestamp(stamp, UTC), values=values))
    floor = now.replace(minute=0, second=0, microsecond=0)
    if not any(
        floor <= point.timestamp < floor + timedelta(hours=48)
        and any(value is not None for value in point.values.values())
        for point in points
    ):
        raise ValueError("No usable upcoming forecast")
    return Snapshot(
        fetched_at=now,
        grid=Coordinates(latitude=payload["latitude"], longitude=payload["longitude"]),
        units=VARIABLES[source],
        hourly=points,
    )


def retry_after(value: str | None, now: datetime, fallback: float) -> datetime:
    try:
        seconds = float(value)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError
        return now + timedelta(seconds=seconds)
    except TypeError, ValueError, OverflowError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(now, parsed.astimezone(UTC))
        except TypeError, ValueError, OverflowError:
            return now + timedelta(seconds=fallback)


class OpenMeteo:
    def __init__(self, client, settings, metrics, clock):
        self.client, self.settings, self.metrics, self.clock = client, settings, metrics, clock
        self.semaphore = asyncio.Semaphore(4)

    async def fetch(self, location: Location, source: Source) -> Snapshot:
        params = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "hourly": ",".join(VARIABLES[source]),
            "timezone": "UTC",
            "timeformat": "unixtime",
            "forecast_days": 3,
        }
        if source == Source.MARINE:
            params["cell_selection"] = "sea"
        else:
            params.update(
                temperature_unit="celsius", wind_speed_unit="kmh", precipitation_unit="mm"
            )
        url = getattr(self.settings, f"{source}_base_url")
        url += "/v1/marine" if source == Source.MARINE else "/v1/forecast"
        for attempt in range(2):
            outcome, retryable = "internal_error", False
            async with self.semaphore:
                start = time.perf_counter()
                try:
                    async with asyncio.timeout(self.settings.request_timeout_seconds):
                        response = await self.client.get(url, params=params)
                    if response.status_code == 429:
                        outcome = "rate_limited"
                        raise ProviderError(
                            outcome,
                            retry_after(
                                response.headers.get("Retry-After"),
                                self.clock(),
                                self.settings.refresh_seconds,
                            ),
                        )
                    if response.status_code >= 400:
                        outcome = "http_error"
                        retryable = response.status_code >= 500
                    else:
                        try:
                            result = normalize(response.json(), source, self.clock())
                            outcome = "success"
                        except ValueError, TypeError, KeyError, OverflowError:
                            outcome = "invalid_payload"
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                except TimeoutError, httpx.TimeoutException:
                    outcome, retryable = "timeout", True
                except httpx.TransportError:
                    outcome, retryable = "network_error", True
                finally:
                    duration = time.perf_counter() - start
                    self.metrics.upstream.labels(source, outcome).inc()
                    self.metrics.upstream_duration.labels(source).observe(duration)
                    log.info(
                        "upstream_attempt",
                        extra={
                            "fields": {
                                "location_id": location.id,
                                "source": source,
                                "outcome": outcome,
                                "attempt": attempt + 1,
                                "duration_seconds": duration,
                            }
                        },
                    )
            if outcome == "success":
                return result
            if not retryable or attempt == 1:
                raise ProviderError(outcome)
            await asyncio.sleep(self.settings.retry_delay_seconds * random.uniform(1, 2))
        raise AssertionError("Unreachable retry state")
