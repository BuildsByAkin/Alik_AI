"""Application configuration, loaded from the environment (prefix ``ALIK_``)."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ALIK_", env_file=".env", extra="ignore")

    # Runtime LLM — the model the companion calls. Never hardcode the string elsewhere.
    anthropic_api_key: SecretStr = SecretStr("")
    companion_model: str = "claude-sonnet-4-6"
    companion_max_tokens: int = 1024

    # Extraction LLM — the cheap model that mines transcripts into graph nodes.
    extraction_model: str = "claude-haiku-4-5-20251001"
    extraction_max_tokens: int = 2048

    # Infrastructure.
    database_url: str = "postgresql://alik:alik@localhost:5432/alik"
    redis_url: str = "redis://localhost:6379/0"
    # FalkorDB runs as its own service (host 6380) so Phase 1's Redis is untouched.
    falkordb_url: str = "redis://localhost:6380/0"
    graph_name: str = "alik"

    # Memory tuning.
    working_buffer_ttl_seconds: int = 21600  # ~6h; abandoned session buffers self-expire.
    episode_retrieve_limit: int = 10  # how many recent episodic summaries to inject.
    current_facts_limit: int = 50  # how many current graph nodes (per type) to inject.

    # Sleep pass (Phase 3). Cheap model reuses extraction_model.
    promote_window_days: int = 7  # PROMOTE: scan episodes from the last N days.
    promote_threshold: float = 0.7  # PROMOTE: salience score above which we promote.
    decay_after_days: int = 30  # DECAY: soft-delete non-promoted episodes older than N days.
    confidence_decay_days: int = 60  # DECAY: facts unmentioned this long lose confidence.
    confidence_decay_factor: float = 0.85  # DECAY: multiply stale-fact confidence by this.
    confidence_floor: float = 0.1  # DECAY: never decay confidence below this.
    active_user_window_days: int = 30  # sleep pass runs over users active within N days.
    reflection_after_days: int = 30  # retrieve() injects reflection (not episodes) past this age.
    sleep_pass_hour: int = 2  # scheduler: local hour to run the nightly pass.

    # Pattern layer + reflect-back (Phase 4).
    reflect_back_min_turn: int = 3  # never surface a trait in the first N completed turns.
    reflect_back_min_confidence: float = 0.65  # only surface traits at/above this confidence.
    reflect_back_confidence_bump: float = 0.1  # confirm bumps a trait's confidence by this.
    corrected_trait_confidence: float = 0.7  # confidence of a user-corrected (new) trait.
    trait_stale_days: int = 14  # PRUNE: close inferred traits not re-detected in N days.
    reflect_back_cooldown_sessions: int = 3  # sessions to wait before reflect-back can refire.

    # Commitments + proactivity (Phase 5).
    commitment_due_fallback_days: int = 14  # TICK: no-expected_by commitments go due after N days.
    proactivity_lapsed_days: int = 3  # general check-in if no session in N days.
    proactivity_upcoming_hours: int = 24  # heads-up window for commitments coming due.
    proactivity_interval_hours: int = 1  # scheduler: how often the proactivity engine runs.

    # Earn / job matching (Phase 7).
    job_catalog_path: str = "data/jobs.json"  # source-of-truth catalog; add a job = add JSON.
    job_match_threshold: float = 0.5  # MATCH: minimum specific-job score to recommend.
    job_match_enabled: bool = True  # false disables both job passes with no code change.
    job_followup_after_days: int = 3  # wait this long after delivery before following up.
    job_disliked_cooldown_days: int = 14  # after tried_disliked, recommend nothing for N days.
    job_not_tried_cooldown_days: int = 7  # after not_tried, wait N days then a different category.

    # Optional persona override; falls back to the packaged persona.txt.
    persona_path: str | None = None
