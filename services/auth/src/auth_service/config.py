"""Service configuration, loaded from the environment (prefix ``SUPABASE_``).

The three Supabase keys use the ``SUPABASE_`` prefix; ``PORT`` is read without it.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SUPABASE_", env_file=".env", extra="ignore")

    # Supabase project (Settings → API). url has no SUPABASE_ duplication issue because the
    # field name already carries it: env var is SUPABASE_URL → field `url`.
    url: str = ""
    anon_key: SecretStr = SecretStr("")
    service_key: SecretStr = SecretStr("")


class ServerSettings(BaseSettings):
    """Non-Supabase server settings (read without the SUPABASE_ prefix)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    port: int = 8001
    # Shared secret for the service-to-service /internal endpoints (the brain calls these
    # to read identity for the living profile and to coordinate account erasure). When
    # empty, the /internal endpoints reject every request (fail closed).
    service_token: SecretStr = SecretStr("")


settings = Settings()
server_settings = ServerSettings()
