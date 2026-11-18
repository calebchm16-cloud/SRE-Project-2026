"""Environment configuration. Location IDs bound API and telemetry cardinality."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, TypeAdapter, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import VARIABLES, Location, Source


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAHUITA_", env_file=".env", extra="ignore", populate_by_name=True
    )
    locations_file: Path = Path(__file__).with_name("locations.json")
    database_path: Path = Path("data/cache.sqlite3")
    weather_base_url: str = "https://api.open-meteo.com"
    marine_base_url: str = "https://marine-api.open-meteo.com"
    refresh_seconds: float = Field(default=900, gt=0)
    stale_seconds: float = Field(default=1800, gt=0)
    expire_seconds: float = Field(default=21600, gt=0)
    request_timeout_seconds: float = Field(default=10, gt=0)
    retry_delay_seconds: float = Field(default=0.5, ge=0)
    heartbeat_seconds: float = Field(default=1, gt=0)
    otlp_traces_endpoint: str | None = Field(
        default=None, validation_alias="OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
    )
    trace_sample_ratio: float = Field(default=1, ge=0, le=1)
    log_level: str = "INFO"

    @field_validator("weather_base_url", "marine_base_url", "otlp_traces_endpoint")
    @classmethod
    def valid_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
            raise ValueError("Expected an HTTP(S) URL without embedded credentials")
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def valid_level(cls, value: str) -> str:
        if value.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid log level")
        return value.upper()

    @model_validator(mode="after")
    def freshness_order(self) -> Settings:
        if not self.refresh_seconds < self.stale_seconds < self.expire_seconds:
            raise ValueError("Require refresh_seconds < stale_seconds < expire_seconds")
        return self


def load_locations(path: Path) -> list[Location]:
    locations = TypeAdapter(list[Location]).validate_json(path.read_text())
    if not locations or len({loc.id for loc in locations}) != len(locations):
        raise ValueError("Configure at least one location, with unique IDs")
    return locations


def fingerprint(location: Location, source: Source, settings: Settings) -> str:
    data = {
        "schema": 1,
        "location": location.model_dump(),
        "source": source,
        "variables": VARIABLES[source],
        "base_url": getattr(settings, f"{source}_base_url"),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
