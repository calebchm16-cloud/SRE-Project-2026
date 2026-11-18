"""Public response models and the validated, persisted snapshot contract."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class Source(StrEnum):
    WEATHER = "weather"
    MARINE = "marine"


VARIABLES: dict[Source, dict[str, str]] = {
    Source.WEATHER: {
        "temperature_2m": "°C",
        "precipitation_probability": "%",
        "precipitation": "mm",
        "wind_speed_10m": "km/h",
        "wind_direction_10m": "°",
        "wind_gusts_10m": "km/h",
    },
    Source.MARINE: {"wave_height": "m", "wave_period": "s", "wave_direction": "°"},
}


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    name: str = Field(min_length=1, max_length=100)
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    timezone: str

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Use a valid IANA timezone") from exc
        return value


class Coordinates(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class HourlyPoint(BaseModel):
    timestamp: AwareDatetime
    values: dict[str, float | None]


class Snapshot(BaseModel):
    fetched_at: AwareDatetime
    grid: Coordinates
    units: dict[str, str]
    hourly: list[HourlyPoint]


class CacheState(BaseModel):
    snapshot: Snapshot | None = None
    last_attempt_at: AwareDatetime | None = None
    last_outcome: str = "never"
    last_error: str | None = None
    next_allowed_at: AwareDatetime | None = None


class SourceStatus(BaseModel):
    source: str = "open-meteo"
    endpoint: Source
    freshness: Literal["fresh", "stale", "unavailable"]
    reason: str | None = None
    last_success_at: AwareDatetime | None = None
    last_attempt_at: AwareDatetime | None = None
    last_outcome: str
    last_error: str | None = None
    next_allowed_at: AwareDatetime | None = None
    cache_age_seconds: float | None = None
    requested_coordinates: Coordinates
    grid_coordinates: Coordinates | None = None
    coverage_start: AwareDatetime | None = None
    coverage_end: AwareDatetime | None = None
    available_hours: int = 0
    complete: bool = False


class ForecastSection(SourceStatus):
    units: dict[str, str]
    hourly: list[HourlyPoint] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class ForecastResponse(BaseModel):
    location: Location
    generated_at: AwareDatetime
    status: Literal["ok", "degraded", "unavailable"]
    weather: ForecastSection
    marine: ForecastSection
    error: ErrorDetail | None = None


class LocationStatus(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    weather: SourceStatus
    marine: SourceStatus


class ApplicationStatus(BaseModel):
    status: Literal["ok", "degraded", "unavailable"]
    generated_at: AwareDatetime
    storage_healthy: bool
    refresh_loop_healthy: bool
    refresh_loop_last_tick: datetime | None
    ready: bool
    locations: dict[str, LocationStatus]


class HealthResponse(BaseModel):
    status: str
    reasons: list[str] = Field(default_factory=list)
