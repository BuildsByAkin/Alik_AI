"""Service configuration, loaded from the environment (plain field names, no prefix —
mirrors services/matching). Built once at import as a singleton."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from connections_service.models import AgeFilterMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # This service's own Postgres (all match state lives here).
    database_url: str = "postgresql://alik:alik@localhost:5434/connections"
    # The companion brain (source of the assembled living profile — the only user read).
    brain_url: str = "http://localhost:8000"
    # The auth service (source of the ingest roster: which user_ids exist, by state).
    auth_url: str = "http://localhost:8001"
    # Shared service-to-service secret (== brain ALIK_SERVICE_TOKEN == auth/matching SERVICE_TOKEN).
    service_token: SecretStr = SecretStr("")
    port: int = 8003

    # Ingestion (Part 2). APScheduler cron; refresh every 6h by default.
    ingest_cron: str = "0 */6 * * *"
    launch_states: str = "MN"  # CSV of 2-letter codes to ingest (auth owns the real gate).

    # Pool-readiness floor: a user is pool_ready with >=1 interest edge OR >=1 dimension at/above
    # this confidence. trait_confidence_floor gates interest extraction from confirmed traits.
    dimension_confidence_floor: float = 0.6
    trait_confidence_floor: float = 0.7

    # Compatibility kernel (Part 3). Component weights are renormalized over present
    # components, so missing dimensions/values never cap the score.
    interest_weight: float = 0.5
    dimension_weight: float = 0.35
    values_weight: float = 0.15
    broad_interest_multiplier: float = 0.4  # down-weight broad-only overlap vs specific.
    dimension_axis_confidence_floor: float = 0.5  # skip an axis below this (no penalty).
    confidence_target_edges: int = 3  # interest evidence saturates at this many edges.
    confidence_review_threshold: float = 0.5  # human_review_flag = confidence < this.
    top_n_candidates: int = 10
    score_cron: str = "0 2 * * *"  # nightly, after ingest.

    # Cross-evaluation model (Part 4; cheap/Haiku-class, never hardcoded).
    llm_model: str = "claude-haiku-4-5-20251001"
    # Age-handling knob — default OFF. 25+ is gated at signup; the kernel must not read age.
    age_filter_mode: AgeFilterMode = AgeFilterMode.OFF

    @property
    def launch_states_set(self) -> set[str]:
        return {s.strip().upper() for s in self.launch_states.split(",") if s.strip()}


settings = Settings()
