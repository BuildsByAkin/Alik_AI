"""The Memory contract. Nothing outside an implementation imports a DB driver."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from alik.models import (
    JobOutcome,
    JobRecommendation,
    MemoryRecord,
    PendingCheckin,
    RetrievedContext,
)


class Memory(ABC):
    """Single seam for all memory access.

    ``write`` dispatches on ``record.tier``; ``retrieve`` returns both tiers so the
    hot buffer needs no separate access path. Later phases can add tiers without
    changing callers.
    """

    @abstractmethod
    async def write(self, record: MemoryRecord) -> None:
        """Persist a record. WORKING -> hot buffer; EPISODIC -> durable store."""

    @abstractmethod
    async def retrieve(
        self,
        user_id: str,
        session_id: str | None = None,
        *,
        episode_limit: int = 10,
    ) -> RetrievedContext:
        """Return recent episodic summaries plus the session's live buffer.

        ``episode_limit`` keeps a default for direct callers, but the application
        sources its value from configuration (``Settings.episode_retrieve_limit``).
        """

    @abstractmethod
    async def invalidate(self, user_id: str, session_id: str) -> None:
        """Drop the working buffer for one session (e.g. after summarizing it)."""

    @abstractmethod
    async def delete(self, user_id: str) -> None:
        """Hard-delete EVERYTHING for a user (working + episodic). Legal requirement."""

    # --- Phase 3: episodic lifecycle for the nightly sleep pass ----------------

    @abstractmethod
    async def get_active_users(self, *, within_days: int = 30) -> list[str]:
        """User ids with at least one episode created within the window."""

    @abstractmethod
    async def get_recent_episodes(self, user_id: str, *, days: int = 7) -> list[MemoryRecord]:
        """Recent, non-decayed, NOT-yet-promoted episodes — the promotion candidates.

        Already-promoted episodes are excluded so a repeat sleep pass neither
        re-scores them (cost) nor re-promotes them (idempotency). Records carry ``id``.
        """

    @abstractmethod
    async def get_promoted_episodes(self, user_id: str, *, limit: int = 20) -> list[MemoryRecord]:
        """Promoted (durable) episodes, most recent first — input to the reflection."""

    @abstractmethod
    async def promote_episode(self, episode_id: str) -> None:
        """Mark one episode promoted=true. Idempotent."""

    @abstractmethod
    async def decay_episodes(self, user_id: str, *, older_than_days: int = 30) -> int:
        """Soft-delete (set decayed_at) non-promoted episodes older than N days.

        Never hard-deletes (audit trail). Idempotent: rows already decayed are
        skipped. Returns how many rows were newly decayed.
        """

    @abstractmethod
    async def save_reflection(self, user_id: str, content: str) -> None:
        """Store a reflection. At most one per user per UTC day (idempotent re-runs)."""

    @abstractmethod
    async def get_reflection(self, user_id: str) -> str | None:
        """The most recent reflection for the user, or None."""

    # --- Phase 5: proactive check-in queue ------------------------------------

    @abstractmethod
    async def queue_checkin(self, checkin: PendingCheckin) -> None:
        """Persist a queued proactive opener (one undelivered per user — enforced upstream)."""

    @abstractmethod
    async def get_pending_checkin(self, user_id: str) -> PendingCheckin | None:
        """The most recent UNDELIVERED check-in for the user, or None."""

    @abstractmethod
    async def mark_checkin_delivered(self, checkin_id: str) -> None:
        """Stamp delivered_at so the opener fires at most once."""

    @abstractmethod
    async def get_last_session_at(self, user_id: str) -> datetime | None:
        """Timestamp of the user's most recent episodic memory, or None (lapse detection)."""

    # --- Phase 5.2: reflect-back cadence cooldown -----------------------------

    @abstractmethod
    async def reflect_back_ready(self, user_id: str) -> bool:
        """True when no cooldown is active (remaining == 0 / no row)."""

    @abstractmethod
    async def set_reflect_back_cooldown(self, user_id: str, sessions: int) -> None:
        """Start the cooldown: skip reflect-back for the next ``sessions`` sessions."""

    @abstractmethod
    async def decrement_reflect_back_cooldown(self, user_id: str) -> None:
        """Count one session toward clearing the cooldown (floored at 0)."""

    # --- Phase 7: earn / job-matching log -------------------------------------

    @abstractmethod
    async def log_job_recommendation(
        self, user_id: str, job_id: str, *, follow_up_after_days: int
    ) -> str:
        """Record a queued recommendation; schedule follow_up_after = now + N days.

        Returns the new row id. Opening a thread — blocks new recommendations until resolved.
        """

    @abstractmethod
    async def get_recommended_job_ids(self, user_id: str) -> list[str]:
        """Job ids already recommended to this user (dedup — never repeat a job)."""

    @abstractmethod
    async def get_job_recommendations(self, user_id: str) -> list[JobRecommendation]:
        """All of the user's recommendation rows, newest first (drives gating/cooldowns)."""

    @abstractmethod
    async def mark_job_recommendation_delivered(self, rec_id: str) -> None:
        """Stamp delivered_at when the companion shows the recommendation."""

    @abstractmethod
    async def get_due_job_followup(self, user_id: str) -> JobRecommendation | None:
        """A delivered recommendation past its follow_up_after with no follow-up sent yet."""

    @abstractmethod
    async def mark_job_followup_sent(self, rec_id: str) -> None:
        """Stamp follow_up_sent_at when the follow-up check-in is queued."""

    @abstractmethod
    async def get_pending_job_followup(self, user_id: str) -> JobRecommendation | None:
        """The row whose follow-up was queued and is awaiting an outcome (for the companion)."""

    @abstractmethod
    async def update_job_outcome(self, rec_id: str, outcome: JobOutcome) -> None:
        """Record how a recommendation turned out — closes the open thread."""

    @abstractmethod
    async def set_job_active(self, user_id: str, active: bool = True) -> None:
        """Flag that the user has engaged with paid work (tried + liked at least once)."""

    @abstractmethod
    async def get_job_active(self, user_id: str) -> bool:
        """Whether the user's job thread is active (default False)."""
