"""Domain types for the rendezvous (meeting-coordination) service.

A ``Meet`` is one accepted introduction being coordinated into a real meeting. It holds BOTH
sides' state inline (a/b), but every side only ever learns the OTHER person anonymously
(``desc_a`` is what A is told about B — "someone who loves pottery", never a name). The
lifecycle is deliberately small for the MVP: COORDINATING (collect a rough where/when from each
side) → CONFIRMING (relay a rough plan, get a yes/no) → CONFIRMED → FOLLOWED_UP; any decline or
erasure → CANCELLED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel


class MeetStatus(StrEnum):
    COORDINATING = "coordinating"  # collecting each side's rough where/when
    CONFIRMING = "confirming"  # a rough plan was relayed; awaiting both yes/no
    CONFIRMED = "confirmed"  # both agreed — a meet is set
    FOLLOWED_UP = "followed_up"  # both told us how it felt
    CANCELLED = "cancelled"  # someone declined, or a participant was erased

    @property
    def is_active(self) -> bool:
        return self in (MeetStatus.COORDINATING, MeetStatus.CONFIRMING, MeetStatus.CONFIRMED)


@dataclass(frozen=True, slots=True)
class Meet:
    """One meet being coordinated. a/b are the two participants; desc_* is the anonymized thing
    each is told about the other. Updated via ``dataclasses.replace`` keyed by side."""

    user_a: str
    user_b: str
    desc_a: str  # what A is told about B (anonymized)
    desc_b: str  # what B is told about A (anonymized)
    status: MeetStatus = MeetStatus.COORDINATING
    pref_a: str | None = None
    pref_b: str | None = None
    pref_asked_a: bool = False
    pref_asked_b: bool = False
    plan: str | None = None  # the rough relayed plan once both prefs are in
    confirm_a: bool | None = None
    confirm_b: bool | None = None
    confirm_asked_a: bool = False
    confirm_asked_b: bool = False
    followup_a: bool | None = None  # how it felt for A (True = positive)
    followup_b: bool | None = None
    followup_asked_a: bool = False
    followup_asked_b: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    id: str = field(default_factory=lambda: uuid4().hex)

    # --- per-side accessors (so the lifecycle stays symmetric) ----------------
    def side(self, user_id: str) -> str | None:
        if user_id == self.user_a:
            return "a"
        if user_id == self.user_b:
            return "b"
        return None

    def counterpart(self, user_id: str) -> str:
        return self.user_b if user_id == self.user_a else self.user_a

    def descriptor_for(self, user_id: str) -> str:
        return self.desc_a if user_id == self.user_a else self.desc_b

    def pref_of(self, side: str) -> str | None:
        return self.pref_a if side == "a" else self.pref_b

    @property
    def both_prefs_in(self) -> bool:
        return self.pref_a is not None and self.pref_b is not None

    @property
    def both_confirmed(self) -> bool:
        return self.confirm_a is True and self.confirm_b is True

    @property
    def any_declined(self) -> bool:
        return self.confirm_a is False or self.confirm_b is False

    @property
    def both_followed_up(self) -> bool:
        return self.followup_a is not None and self.followup_b is not None


# --- HTTP bodies ----------------------------------------------------------------------------


class CreateMeet(BaseModel):
    """Create a meet (called when both sides accepted a connections intro)."""

    user_a: str
    user_b: str
    desc_a: str  # anonymized descriptor of B, shown to A
    desc_b: str  # anonymized descriptor of A, shown to B


class PrefReply(BaseModel):
    meet_id: str
    user_id: str
    text: str  # free-text rough where/when (relayed as-is in the MVP; no parsing yet)


class ConfirmReply(BaseModel):
    meet_id: str
    user_id: str
    accepted: bool


class FollowupReply(BaseModel):
    meet_id: str
    user_id: str
    felt_positive: bool


class HealthResponse(BaseModel):
    status: str
