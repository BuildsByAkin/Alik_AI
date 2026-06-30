"""Domain types for the connections service (no FastAPI, no DB here).

Part 2 adds the ingestion + interest-graph types. Edges/dimensions are returned without a
``user_id`` (it's the contextual key the Store methods take/return alongside them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class AgeFilterMode(StrEnum):
    """How age affects matching. 25+ is already the signup gate (auth's job); after that age
    does NOT affect who matches with whom. Default OFF; the knob exists only in case it's ever
    needed — the Part-3 kernel must not read age."""

    OFF = "off"  # ignore age entirely (default)
    SOFT = "soft"  # (reserved) down-weight large age gaps
    HARD = "hard"  # (reserved) exclude beyond a configured band


class HealthResponse(BaseModel):
    status: str


@dataclass(frozen=True, slots=True)
class InterestNode:
    """A node in the bipartite interest graph (the taxonomy). ``id`` = ``"{broad}:{specific}"``."""

    id: str
    broad_category: str
    specific_interest: str
    canonical_label: str


@dataclass(frozen=True, slots=True)
class InterestEdge:
    """A person→interest edge. ``user_id`` is the contextual key (passed to/returned by Store)."""

    interest_node_id: str
    weight: float
    source_fact_key: str


@dataclass(frozen=True, slots=True)
class UserPoolEntry:
    """One ingested user snapshot. ``age`` is stored for the record only — NOT a matching signal."""

    user_id: str
    state: str
    age: int | None = None
    city: str | None = None
    pool_ready: bool = False
    last_ingested_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DimensionSnapshot:
    """A per-user behavioral-dimension snapshot (the non-sensitive structured layer)."""

    dimension: str
    value: str
    confidence: float
    status: str


@dataclass(frozen=True, slots=True)
class SharedInterests:
    """Exact specific-node overlap + shared broad categories (the cold-start broadening
    fallback when ``specific`` is empty)."""

    specific: list[InterestNode]
    broad: list[str]


# --- Part 3: the compatibility kernel's output -----------------------------------------

MatchType = Literal["specific", "broad_only", "values_only", "none"]
ScoringMode = Literal["similarity", "compatibility"]


@dataclass(frozen=True, slots=True)
class InterestMatch:
    """One shared specific interest node, with each user's edge weight."""

    node_id: str
    broad_category: str
    specific_interest: str
    weight_a: float
    weight_b: float


@dataclass(frozen=True, slots=True)
class DimensionMatch:
    """One behavioral axis compared, with its per-axis score and which mode produced it."""

    axis: str
    value_a: str
    value_b: str
    axis_score: float
    scoring_mode: ScoringMode


@dataclass(frozen=True, slots=True)
class KernelExplanation:
    """Structured 'why' behind a CandidateScore (stored as jsonb, never a string)."""

    interest_specific: list[InterestMatch] = field(default_factory=list)
    interest_broad: list[str] = field(default_factory=list)  # shared broad categories
    dimensions: list[DimensionMatch] = field(default_factory=list)
    values_causes: list[str] = field(default_factory=list)  # shared social_causes node ids
    match_type: MatchType = "none"


@dataclass(frozen=True, slots=True)
class CandidateScore:
    """The kernel's verdict on a directed pair (subject A → candidate B)."""

    user_id_a: str
    user_id_b: str
    score: float
    interest_score: float
    dimension_score: float
    values_score: float
    confidence: float
    human_review_flag: bool
    explanation: KernelExplanation
    scored_at: datetime | None = None


# --- Part 4: the LLM cross-evaluation's output -----------------------------------------


@dataclass(frozen=True, slots=True)
class EvalResult:
    """The LLM's judgment on a directed pair, combined with the kernel confidence."""

    user_id_a: str
    user_id_b: str
    would_click: bool
    llm_confidence: float
    final_confidence: float  # kernel_conf_weight*kernel + llm_conf_weight*llm (computed on save)
    reason: str
    eval_model: str
    flag_for_review: bool = False
    flag_reason: str | None = None
    evaled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SurfaceableMatch:
    """A match ready for Part 5 to introduce: kernel + LLM signals + the structured 'why'."""

    user_id_a: str
    user_id_b: str
    kernel_score: float
    llm_confidence: float
    final_confidence: float
    reason: str
    explanation: KernelExplanation


# --- Part 5: surfacing + match state ----------------------------------------------------


class MatchStatus(StrEnum):
    """Lifecycle of a surfaced pair (the subject's view of one candidate)."""

    PENDING = "pending"  # decided to surface, not yet queued (transient; not persisted)
    SHOWN = "shown"  # PendingCheckin queued in the brain; companion will deliver it
    ACCEPTED = "accepted"  # user said yes through the companion
    SKIPPED = "skipped"  # user said no / delivered-no-response


@dataclass(frozen=True, slots=True)
class MatchStateEntry:
    user_id: str
    candidate_id: str
    status: MatchStatus
    checkin_id: str | None = None  # the brain PendingCheckin id
    surfaced_at: datetime | None = None
    responded_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MatchCheckin:
    """What we send the brain to queue a people-match opener (the EvalResult reason is the core)."""

    candidate_id: str
    reason: str
    shared_interests: list[str]
    match_confidence: float

    def to_payload(self) -> dict:
        return {
            "type": "people_match",
            "reason": self.reason,
            "candidate_id": self.candidate_id,
            "shared_interests": list(self.shared_interests),
            "match_confidence": self.match_confidence,
        }


class MatchResponse(BaseModel):
    """The companion's callback body for POST /matches/response."""

    user_id: str
    candidate_id: str
    accepted: bool


# --- Part 6: group-awareness clustering -------------------------------------------------


class GroupStatus(StrEnum):
    """Lifecycle of a group candidate."""

    PROPOSED = "proposed"  # clustering found it, not yet surfaced
    SURFACING = "surfacing"  # check-ins to members in progress
    SURFACED = "surfaced"  # all members received the intro
    DECLINED = "declined"  # enough members said no (default: any one)


@dataclass(frozen=True, slots=True)
class GroupCandidate:
    group_id: str
    interest_node_id: str
    member_ids: list[str]  # sorted (drives dedup)
    mean_score: float
    status: GroupStatus
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GroupCheckin:
    """What we send the brain to introduce a member to the rest of a group."""

    group_id: str
    candidate_ids: list[str]  # the OTHER members (never self)
    shared_interest: str
    reason: str
    match_confidence: float

    def to_payload(self) -> dict:
        return {
            "type": "people_match_group",
            "group_id": self.group_id,
            "reason": self.reason,
            "candidate_ids": list(self.candidate_ids),
            "shared_interest": self.shared_interest,
            "match_confidence": self.match_confidence,
        }


class GroupResponse(BaseModel):
    """The companion's callback body for POST /matches/group-response."""

    user_id: str
    group_id: str
    accepted: bool
