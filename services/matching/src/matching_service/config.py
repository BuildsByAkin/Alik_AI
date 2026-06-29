"""Service configuration, loaded from the environment.

The Supabase-style three-key prefix doesn't apply here; everything is read by its plain
field name (DATABASE_URL, BRAIN_URL, SERVICE_TOKEN, PORT, CATALOG_PATH) plus the job
tuning knobs (moved verbatim from the brain's Phase 7 settings).
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # This service's own Postgres.
    database_url: str = "postgresql://alik:alik@localhost:5433/matching"
    # The companion brain (source of the assembled living profile used for scoring).
    brain_url: str = "http://localhost:8000"
    # Shared service-to-service secret (== brain ALIK_SERVICE_TOKEN == auth SERVICE_TOKEN).
    service_token: SecretStr = SecretStr("")
    port: int = 8002

    # Catalog + matching tuning (moved from the brain's Phase 7 settings).
    catalog_path: str = "data/jobs.json"
    job_match_threshold: float = 0.5  # minimum specific-job score to recommend.
    job_followup_after_days: int = 3  # wait this long after delivery before following up.
    job_disliked_cooldown_days: int = 14  # after tried_disliked, recommend nothing for N days.
    job_not_tried_cooldown_days: int = 7  # after not_tried, wait N days then a different category.


settings = Settings()
