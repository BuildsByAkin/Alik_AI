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

    # LLM cross-evaluation (Part 4). Model is env-configurable, never hardcoded.
    anthropic_api_key: SecretStr = SecretStr("")
    eval_model: str = "claude-haiku-4-5-20251001"
    eval_max_tokens: int = 512
    eval_cron: str = "0 3 * * *"  # nightly, one hour after scoring.
    min_kernel_score: float = 0.45  # only cross-eval candidates at/above this kernel score.
    eval_top_n: int = 5  # ...and only the top N per user.
    # final_confidence = kernel_conf_weight*kernel + llm_conf_weight*llm.
    kernel_conf_weight: float = 0.6
    llm_conf_weight: float = 0.4
    # A match surfaces when would_click AND final_confidence >= surface_threshold.
    surface_threshold: float = 0.55
    # Render a shared dimension only at/above this axis score; cap interests in a summary.
    shared_dimension_threshold: float = 0.7
    summary_max_interests: int = 8

    # Surfacing (Part 5). One introduction at a time so the companion isn't flooded.
    surface_cron: str = "0 4 * * *"  # nightly, one hour after eval.
    max_surface_per_pass: int = 1
    surface_shared_interests: int = 3  # how many shared interests to send in the checkin payload.

    # Group clustering (Part 6). Lower score bar than 1:1 — shared activity carries weight.
    cluster_cron: str = "0 5 * * *"  # nightly, one hour after surface.
    min_group_size: int = 3
    max_group_size: int = 5
    group_score_threshold: float = 0.5  # every pair in a group must score at/above this.
    group_decline_threshold: int = 1  # this many declines decline the whole group (1 = any).

    # Age-handling knob — default OFF. 25+ is gated at signup; the kernel must not read age.
    age_filter_mode: AgeFilterMode = AgeFilterMode.OFF

    # Monitoring (pass-run digest + alerting). The digest aggregates pass_runs over a window;
    # the alert fires when the eval pass's LLM-failure rate is at/above the threshold (usually an
    # Anthropic outage, not a data problem). digest_cron is for the optional in-process scheduler.
    digest_window_hours: int = 24
    eval_error_rate_threshold: float = 0.2
    digest_cron: str = "30 6 * * *"  # once a day, after the overnight cron cycle

    @property
    def launch_states_set(self) -> set[str]:
        return {s.strip().upper() for s in self.launch_states.split(",") if s.strip()}


settings = Settings()
