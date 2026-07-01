"""Service configuration (plain field names, no prefix — mirrors matching/connections)."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # This service's own Postgres (all meet state lives here — never the brain's DBs).
    database_url: str = "postgresql://alik:alik@localhost:5435/rendezvous"
    # The companion brain — where we queue coordination check-ins and record social events.
    brain_url: str = "http://localhost:8000"
    # Shared service-to-service secret (== brain ALIK_SERVICE_TOKEN == the other SERVICE_TOKENs).
    service_token: SecretStr = SecretStr("")
    port: int = 8004

    # The advance pass (drives the meet lifecycle by queuing check-ins). Runs often — it only
    # acts on meets that need the next nudge and is idempotent per stage (asked-flags guard).
    advance_cron: str = "*/30 * * * *"  # every 30 minutes


settings = Settings()
