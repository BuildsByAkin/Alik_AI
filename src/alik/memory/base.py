"""The Memory contract. Nothing outside an implementation imports a DB driver."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from alik.models import (
    MemoryRecord,
    PendingCheckin,
    ProfileDimension,
    RetrievedContext,
    SocialEvent,
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
    async def queue_checkin(self, checkin: PendingCheckin) -> str:
        """Persist a queued proactive opener; returns its id (one undelivered per user)."""

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

    # --- Living profile: behavioral dimensions --------------------------------

    @abstractmethod
    async def get_profile_dimensions(self, user_id: str) -> list[ProfileDimension]:
        """All current profile dimensions for the user (any status)."""

    @abstractmethod
    async def put_profile_dimension(self, dimension: ProfileDimension) -> None:
        """Upsert a dimension by (user_id, dimension).

        The accumulation policy lives in ``alik.profile.apply_observation``; this just
        persists the already-merged row. ``valid_from`` (first observed) is preserved
        across upserts.
        """

    @abstractmethod
    async def get_dimension_to_confirm(
        self, user_id: str, session_id: str, *, min_confidence: float, min_observations: int
    ) -> ProfileDimension | None:
        """An UNCONFIRMED dimension worth a gentle check: confident + observed enough,
        and not already surfaced in this session. Highest-confidence first, else None."""

    @abstractmethod
    async def confirm_dimension(
        self, user_id: str, dimension: str, *, confidence_bump: float, session_id: str | None = None
    ) -> None:
        """User agreed in conversation — mark CONFIRMED and bump confidence."""

    @abstractmethod
    async def correct_dimension(
        self, user_id: str, dimension: str, *, session_id: str | None = None
    ) -> None:
        """User disagreed — mark CORRECTED (never drives behavior, never re-surfaced)."""

    @abstractmethod
    async def mark_dimension_surfaced(self, user_id: str, dimension: str, session_id: str) -> None:
        """Record that we soft-confirmed this dimension in ``session_id``."""

    # --- Phase 8: matchmaking write-back (social events) ----------------------

    @abstractmethod
    async def record_social_event(self, event: SocialEvent) -> None:
        """Persist a durable, per-user matchmaking event (intro / accept / meet / job) so the
        companion stays coherent about its own matchmaking. Erased by ``delete``."""

    @abstractmethod
    async def get_recent_social_events(self, user_id: str, *, limit: int = 10) -> list[SocialEvent]:
        """The user's most recent social events, newest first."""
