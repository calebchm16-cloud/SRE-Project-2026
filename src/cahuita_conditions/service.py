"""Persistent source snapshots and an independently supervised refresh scheduler."""

import asyncio
import logging
from datetime import timedelta

from opentelemetry.trace import Status, StatusCode

from .config import fingerprint
from .models import (
    VARIABLES,
    ApplicationStatus,
    CacheState,
    Coordinates,
    ErrorDetail,
    ForecastResponse,
    ForecastSection,
    LocationStatus,
    Source,
    SourceStatus,
)
from .provider import ProviderError

log = logging.getLogger("cahuita.refresh")


class ForecastService:
    def __init__(self, settings, locations, store, provider, metrics, tracer, clock):
        self.settings, self.locations, self.store = settings, locations, store
        self.provider, self.metrics, self.tracer, self.clock = provider, metrics, tracer, clock
        self.signatures = {
            (loc.id, source): fingerprint(loc, source, settings)
            for loc in locations
            for source in Source
        }
        self.states = {key: CacheState() for key in self.signatures}
        self.next_due = {key: clock() for key in self.states}
        self.in_flight = {}
        self.task = None
        self.last_tick = None
        self.storage_healthy = True

    async def start(self, auto_refresh=True):
        self.states.update(await self.store.load(self.signatures))
        self.task = asyncio.create_task(self._loop(auto_refresh), name="forecast-scheduler")
        self.task.add_done_callback(self._scheduler_done)

    @staticmethod
    def _scheduler_done(task):
        if not task.cancelled() and task.exception() is not None:
            log.error("refresh_loop_failed", exc_info=task.exception())

    async def _loop(self, auto_refresh):
        while True:
            self.last_tick = self.clock()
            self.metrics.tick.set(self.last_tick.timestamp())
            for location in self.locations:
                for source in Source:
                    key = (location.id, source)
                    current = self.in_flight.get(key)
                    backoff = self.states[key].next_allowed_at
                    if (
                        auto_refresh
                        and (current is None or current.done())
                        and self.clock() >= self.next_due[key]
                        and (backoff is None or self.clock() >= backoff)
                    ):
                        self.next_due[key] = self.clock() + timedelta(
                            seconds=self.settings.refresh_seconds
                        )
                        self.in_flight[key] = asyncio.create_task(self.refresh(location, source))
            self.update_metrics()
            await asyncio.sleep(self.settings.heartbeat_seconds)

    async def refresh(self, location, source):
        key = (location.id, source)
        with self.tracer.start_as_current_span(
            "forecast.refresh",
            attributes={
                "forecast.location_id": location.id,
                "forecast.source": str(source),
            },
        ) as span:
            state = self.states[key].model_copy(deep=True)
            state.last_attempt_at = self.clock()
            try:
                state.snapshot = await self.provider.fetch(location, source)
                state.last_outcome, state.last_error, state.next_allowed_at = "success", None, None
            except ProviderError as exc:
                state.last_outcome, state.last_error = exc.outcome, exc.outcome
                state.next_allowed_at = exc.retry_at
                span.set_status(Status(StatusCode.ERROR, exc.outcome))
            except Exception:
                state.last_outcome, state.last_error = "internal_error", "internal_error"
                log.exception("refresh_internal_error")
                span.set_status(Status(StatusCode.ERROR, "internal_error"))
            try:
                await self.store.save(key, self.signatures[key], state)
                self.storage_healthy = True
                self.states[key] = state
            except Exception:
                self.storage_healthy = False
                state.last_outcome = "storage_error"
                span.set_status(Status(StatusCode.ERROR, "storage_error"))
                log.exception("storage_write_failed")
            self.metrics.refresh.labels(*key, state.last_outcome).inc()
            log.info(
                "forecast_refresh",
                extra={
                    "fields": {
                        "location_id": location.id,
                        "source": source,
                        "outcome": state.last_outcome,
                    }
                },
            )
            self.update_metrics()

    def section(self, location, source, now):
        state = self.states[(location.id, source)]
        snap = state.snapshot
        age = max(0, (now - snap.fetched_at).total_seconds()) if snap else None
        floor = now.replace(minute=0, second=0, microsecond=0)
        points = (
            []
            if snap is None
            else [
                point
                for point in snap.hourly
                if floor <= point.timestamp < floor + timedelta(hours=48)
            ]
        )
        available = sum(any(value is not None for value in p.values.values()) for p in points)
        reason = None
        freshness = "fresh"
        if snap is None:
            freshness, reason = "unavailable", "no_snapshot"
        elif age >= self.settings.expire_seconds:
            freshness, reason = "unavailable", "expired"
        elif not available:
            freshness, reason = "unavailable", "no_upcoming_forecast"
        elif age >= self.settings.stale_seconds:
            freshness, reason = "stale", "refresh_overdue"
        if freshness == "unavailable":
            points, available = [], 0
        complete = len(points) == 48 and all(
            all(value is not None for value in point.values.values()) for point in points
        )
        return ForecastSection(
            endpoint=source,
            freshness=freshness,
            reason=reason,
            last_success_at=snap.fetched_at if snap else None,
            last_attempt_at=state.last_attempt_at,
            last_outcome=state.last_outcome,
            last_error=state.last_error,
            next_allowed_at=state.next_allowed_at,
            cache_age_seconds=age,
            requested_coordinates=Coordinates(
                latitude=location.latitude, longitude=location.longitude
            ),
            grid_coordinates=snap.grid if snap else None,
            coverage_start=points[0].timestamp if points else None,
            coverage_end=points[-1].timestamp if points else None,
            available_hours=available,
            complete=complete,
            units=VARIABLES[source],
            hourly=points,
        )

    def forecast(self, location):
        now = self.clock()
        weather, marine = (self.section(location, source, now) for source in Source)
        usable = [part for part in (weather, marine) if part.freshness != "unavailable"]
        status = "unavailable" if not usable else "degraded"
        if len(usable) == 2 and all(
            part.freshness == "fresh" and part.complete and part.last_outcome == "success"
            for part in usable
        ):
            status = "ok"
        return ForecastResponse(
            location=location,
            generated_at=now,
            status=status,
            weather=weather,
            marine=marine,
            error=ErrorDetail(code="forecast_unavailable", message="No usable cached forecast")
            if not usable
            else None,
        )

    async def status(self):
        storage = await self.store.healthy() and self.storage_healthy
        now = self.clock()
        loop = bool(
            self.task
            and not self.task.done()
            and self.last_tick is not None
            and (now - self.last_tick).total_seconds() < max(5, self.settings.heartbeat_seconds * 5)
        )
        forecasts = {loc.id: self.forecast(loc) for loc in self.locations}
        ready = storage and loop and all(f.status != "unavailable" for f in forecasts.values())
        status = (
            "unavailable"
            if not ready
            else ("ok" if all(f.status == "ok" for f in forecasts.values()) else "degraded")
        )
        return ApplicationStatus(
            status=status,
            generated_at=now,
            storage_healthy=storage,
            refresh_loop_healthy=loop,
            refresh_loop_last_tick=self.last_tick,
            ready=ready,
            locations={
                key: LocationStatus(
                    status=f.status,
                    weather=SourceStatus.model_validate(f.weather.model_dump()),
                    marine=SourceStatus.model_validate(f.marine.model_dump()),
                )
                for key, f in forecasts.items()
            },
        )

    def update_metrics(self):
        now = self.clock()
        for location in self.locations:
            for source in Source:
                part = self.section(location, source, now)
                labels = (location.id, source)
                self.metrics.usable.labels(*labels).set(part.freshness != "unavailable")
                self.metrics.hours.labels(*labels).set(part.available_hours)
                if part.last_success_at is not None:
                    self.metrics.last_success.labels(*labels).set(part.last_success_at.timestamp())
                    self.metrics.age.labels(*labels).set(part.cache_age_seconds)

    async def close(self):
        tasks = [task for task in [self.task, *self.in_flight.values()] if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
