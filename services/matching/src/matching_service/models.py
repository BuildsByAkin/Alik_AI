"""Domain types for the matching service (no FastAPI, no DB here)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import uuid4


class JobOutcome(StrEnum):
    """How a delivered recommendation turned out (set from the user's follow-up reply)."""

    TRIED_LIKED = "tried_liked"
    TRIED_DISLIKED = "tried_disliked"  # cool off + never that partner again
    NOT_TRIED = "not_tried"  # cool off, different category next
    LOVED_IT = "loved_it"


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One delivered/queued job thread. ``outcome is None`` means the thread is open."""

    user_id: str
    job_id: str
    recommended_at: datetime
    delivered_at: datetime | None = None
    follow_up_after: datetime | None = None
    follow_up_sent_at: datetime | None = None
    outcome: JobOutcome | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
