"""ASGI application. HTTP handlers never fetch data from an upstream provider."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import __version__
from .config import Settings, load_locations
from .models import (
    ApplicationStatus,
    ErrorResponse,
    ForecastResponse,
    HealthResponse,
    Location,
)
from .provider import OpenMeteo
from .service import ForecastService
from .storage import CacheStore
from .telemetry import Metrics, RequestTelemetry, configure_logging, make_tracer_provider

log = logging.getLogger("cahuita.app")
PACKAGE = Path(__file__).parent


def create_app(settings=None, *, clock=None, transport=None, exporter=None, auto_refresh=True):
    settings = settings or Settings()
    clock = clock or (lambda: datetime.now(UTC))
    locations = load_locations(settings.locations_file)
    metrics = Metrics()
    tracing = make_tracer_provider(settings, exporter)
    templates = Jinja2Templates(directory=PACKAGE / "templates")

    @asynccontextmanager
    async def lifespan(app):
        configure_logging(settings.log_level)
        store = CacheStore(settings.database_path)
        service = None
        client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            transport=transport,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            headers={"User-Agent": f"cahuita-conditions/{__version__}"},
            follow_redirects=False,
        )
        HTTPXClientInstrumentor.instrument_client(client, tracer_provider=tracing)
        try:
            await store.open()
            provider = OpenMeteo(client, settings, metrics, clock)
            service = ForecastService(
                settings,
                locations,
                store,
                provider,
                metrics,
                tracing.get_tracer("cahuita-conditions"),
                clock,
            )
            await service.start(auto_refresh)
            app.state.service = service
            log.info("application_started", extra={"fields": {"version": __version__}})
            yield
        finally:
            if service is not None:
                await service.close()
            HTTPXClientInstrumentor.uninstrument_client(client)
            await client.aclose()
            await store.close()
            if hasattr(tracing, "shutdown"):
                await asyncio.to_thread(tracing.shutdown)
            log.info("application_stopped")

    app = FastAPI(title="Cahuita Conditions", version=__version__, lifespan=lifespan)
    app.state.metrics, app.state.tracing = metrics, tracing
    app.add_middleware(RequestTelemetry, metrics=metrics)
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=tracing, excluded_urls=r".*/health/(live|ready)$,.*/metrics$"
    )
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")

    def get_location(location_id):
        for location in locations:
            if location.id == location_id:
                return location
        raise HTTPException(
            status_code=404,
            detail={
                "code": "unknown_location",
                "message": "Unknown configured location",
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        error = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {
                "code": "http_error",
                "message": str(exc.detail),
            }
        )
        return JSONResponse(status_code=exc.status_code, content={"error": error})

    @app.get("/api/v1/locations", response_model=list[Location])
    async def list_locations():
        return locations

    @app.get(
        "/api/v1/locations/{location_id}/forecast",
        response_model=ForecastResponse,
        responses={404: {"model": ErrorResponse}, 503: {"model": ForecastResponse}},
    )
    async def forecast(location_id: str):
        result = app.state.service.forecast(get_location(location_id))
        return JSONResponse(
            status_code=503 if result.status == "unavailable" else 200,
            content=result.model_dump(mode="json"),
        )

    @app.get("/api/v1/status", response_model=ApplicationStatus)
    async def status():
        return await app.state.service.status()

    @app.get("/health/live", response_model=HealthResponse)
    async def live():
        return HealthResponse(status="alive")

    @app.get(
        "/health/ready", response_model=HealthResponse, responses={503: {"model": HealthResponse}}
    )
    async def ready():
        current = await app.state.service.status()
        reasons = []
        if not current.storage_healthy:
            reasons.append("storage_unhealthy")
        if not current.refresh_loop_healthy:
            reasons.append("refresh_loop_unhealthy")
        if any(loc.status == "unavailable" for loc in current.locations.values()):
            reasons.append("forecast_unavailable")
        return JSONResponse(
            status_code=200 if current.ready else 503,
            content=HealthResponse(
                status="ready" if current.ready else "not_ready",
                reasons=reasons,
            ).model_dump(),
        )

    @app.get("/metrics", include_in_schema=False)
    async def prometheus():
        app.state.service.update_metrics()
        return Response(
            generate_latest(metrics.registry), headers={"Content-Type": CONTENT_TYPE_LATEST}
        )

    @app.get("/", include_in_schema=False)
    async def index(request: Request, location: str | None = None):
        selected = get_location(location) if location is not None else locations[0]
        result = app.state.service.forecast(selected)
        rows = {}
        for section in (result.weather, result.marine):
            for point in section.hourly:
                rows.setdefault(point.timestamp, {}).update(point.values)
        ordered = [
            {
                "time": stamp.astimezone(ZoneInfo(selected.timezone)).strftime("%Y-%m-%d %H:%M"),
                "values": values,
            }
            for stamp, values in sorted(rows.items())
        ]
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "locations": locations,
                "selected": selected,
                "forecast": result,
                "rows": ordered,
                "sources": [result.weather, result.marine],
                "version": __version__,
            },
        )

    return app
