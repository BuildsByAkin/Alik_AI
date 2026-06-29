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
