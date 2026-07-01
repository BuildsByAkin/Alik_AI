"""Modality-independent memory data types. Text in, text out — no I/O here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import uuid4


class MemoryTier(StrEnum):
    WORKING = "working"  # live session hot buffer (ephemeral, Redis)
    EPISODIC = "episodic"  # per-session summaries (durable, Postgres)


class NodeType(StrEnum):
    """Kinds of structured knowledge extracted into the temporal graph."""

    FACT = "fact"  # durable truth about the person ("trail runs on weekends")
    EMOTIONAL_SIGNAL = "emotional_signal"  # point-in-time affect (append-only time-series)
    COMMITMENT = "commitment"  # something the user said they'd do


class TraitStatus(StrEnum):
    """Lifecycle of an InferredTrait (Phase 4 pattern layer)."""

    INFERRED = "inferred"  # detected by the sleep pass; never stated as fact, only surfaced
    CONFIRMED = "confirmed"  # user agreed via reflect-back; safe to inject into the prompt
    CORRECTED = "corrected"  # user disagreed; window closed, superseded by a new trait


class CommitmentStatus(StrEnum):
    """Lifecycle of a Commitment (Phase 5). Only the user resolves; the tick pass
    only advances pending -> due. Existing Phase 2 nodes have no status property and
    are read as ``pending`` (COALESCE in the store)."""

    PENDING = "pending"  # said, not yet due
    DUE = "due"  # expected_by passed, or sat long enough (fallback)
    RESOLVED_KEPT = "resolved_kept"  # user followed through
    RESOLVED_DROPPED = "resolved_dropped"  # user let it go


class DimensionStatus(StrEnum):
    """Lifecycle of a ProfileDimension (the living-profile behavioral layer).

    Mirrors the InferredTrait lifecycle, but for fixed-taxonomy behavioral axes:
    unconfirmed evidence accumulates silently; a soft-confirm in conversation moves a
    dimension to CONFIRMED or CORRECTED. Tracking them separately is what lets the
    companion know which dimensions still need a gentle check."""

    UNCONFIRMED = "unconfirmed"  # inferred from accumulated evidence; not yet checked with the user
    CONFIRMED = "confirmed"  # user agreed via soft-confirm; authoritative for behavior + matching
    CORRECTED = "corrected"  # user disagreed; never drives behavior, never re-surfaced


class CheckinType(StrEnum):
    """Why the proactivity engine queued a check-in (Phase 5; Phase 7 adds the job types)."""

    DUE_COMMITMENT = "due_commitment"  # follow up on something now due
    UPCOMING_COMMITMENT = "upcoming_commitment"  # gentle heads-up before it's due
    GENERAL_CHECKIN = "general_checkin"  # lapsed user; "how are things?", not a commitment
    JOB_RECOMMENDATION = "job_recommendation"  # Phase 7: a paid-work match worth a warm mention
    JOB_FOLLOWUP = "job_followup"  # Phase 7: follow up on a recommendation already delivered
    PEOPLE_MATCH = "people_match"  # the connections service found someone to introduce (Part 5)
    PEOPLE_MATCH_GROUP = (
        "people_match_group"  # ...or a small group around a shared activity (Part 6)
    )
    # Phase 8 (rendezvous): coordinate an accepted introduction into a real meeting.
    RENDEZVOUS_PREF = "rendezvous_pref"  # ask, warmly, roughly where/when they'd like to meet
    RENDEZVOUS_CONFIRM = "rendezvous_confirm"  # relay the other side's suggestion / confirm a plan
    RENDEZVOUS_FOLLOWUP = "rendezvous_followup"  # after the meet, ask how it FELT (not whether)


class JobOutcome(StrEnum):
    """How a delivered job recommendation turned out (Phase 7). Set at follow-up."""

    TRIED_LIKED = "tried_liked"  # tried it, liked it
    TRIED_DISLIKED = "tried_disliked"  # tried it, didn't like it (cool off + never that partner)
    NOT_TRIED = "not_tried"  # didn't try it (cool off, different category next)
    LOVED_IT = "loved_it"  # tried it, loved it


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    user_id: str
    session_id: str
    tier: MemoryTier
    content: str
    role: str | None = None  # "user" / "assistant" for working turns
    created_at: datetime | None = None
    id: str | None = None  # episodic row id (set on read); needed to promote/decay


@dataclass(frozen=True, slots=True)
class GraphNode:
    """A single node in the temporal graph, with a validity window.

    ``valid_until is None`` means the statement is true NOW. Temporal resolution
    (Facts only) closes an old node's window when a contradicting one arrives;
    EmotionalSignals and Commitments are append-only.
    """

    user_id: str
    type: NodeType
    key: str  # canonical entity this is "about" — drives Fact supersession
    content: str
    valid_from: datetime
    valid_until: datetime | None = None  # None = still true now
    confidence: float = 1.0
    source_session_id: str | None = None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    """Which promoted episodes and emotional signals support an InferredTrait.

    Non-negotiable: every trait traces back to the specific evidence that produced
    it, so a wrong inference is fully explainable and correctable. At least one of
    the two lists must be non-empty (a trait with zero provenance is rejected).
    """

    episode_ids: list[str] = field(default_factory=list)
    signal_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InferredTrait:
    """A pattern about the person inferred from accumulated evidence.

    Its own graph label (not a ``NodeType``). Like Facts it has a validity window
    and supersedes by ``key``; unlike Facts it is never stated as truth unless
    ``status`` is CONFIRMED — INFERRED traits surface only via reflect-back.
    """

    user_id: str
    key: str  # canonical slug — same key + new content supersedes
    content: str  # human-readable ("gets anxious before big decisions")
    confidence: float
    valid_from: datetime
    status_updated_at: datetime
    provenance: ProvenanceRecord
    valid_until: datetime | None = None  # None = still active
    status: TraitStatus = TraitStatus.INFERRED
    surfaced_in_session: str | None = None  # last session we reflected it back in
    source_session_id: str | None = None  # session a correction was stated in, if any
    last_detected_at: datetime | None = None  # last time detect() (re)saw this pattern
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class CommitmentNode:
    """A thing the user said they'd do, with a full lifecycle (Phase 5).

    Its own type (no longer a ``GraphNode``): commitments now carry status, an
    expected time, and follow-through. Append-only — re-stating a commitment makes a
    new node; ``status`` handles staleness. Only the user resolves one (via
    conversation); the nightly tick pass only advances pending -> due.
    """

    user_id: str
    key: str
    content: str
    valid_from: datetime
    status: CommitmentStatus = CommitmentStatus.PENDING
    expected_by: datetime | None = None  # when the user said they'd do it (null = unknown)
    resolved_at: datetime | None = None
    follow_through: bool | None = None  # null until resolved
    reminded_count: int = 0
    last_reminded_at: datetime | None = None
    mention_count: int = 1  # how many times this open commitment has been (re)stated
    confidence: float = 1.0
    source_session_id: str | None = None
    valid_until: datetime | None = None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class PendingCheckin:
    """A queued proactive opener (Phase 5). One undelivered per user at a time.

    Stored in Postgres (``pending_checkins``). The companion delivers it at the next
    session open instead of a generic greeting, then marks it delivered.
    """

    user_id: str
    checkin_type: CheckinType
    message_hint: str  # warm one-liner the companion opens with
    commitment_id: str | None = None  # null for general check-ins
    payload: dict | None = None  # structured data for richer check-ins (e.g. a people_match)
    created_at: datetime | None = None
    delivered_at: datetime | None = None  # null = not yet delivered
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class ProfileDimension:
    """One axis of the living profile (the behavioral layer).

    ``dimension`` + ``value`` are drawn from the fixed taxonomy in ``alik.profile``;
    ``content`` is the human-readable sentence the soft-confirm question is phrased from
    (it names the concrete evidence, e.g. "seems intensely into chess specifically").
    Confidence accumulates across nightly observations; ``status`` separates confirmed
    from still-needs-a-check dimensions. Provenance is mandatory, like InferredTraits.
    """

    user_id: str
    dimension: str  # taxonomy axis, e.g. "structure_preference"
    value: str  # taxonomy value, e.g. "needs_structure"
    content: str  # one human-readable sentence (drives the soft-confirm question)
    confidence: float
    valid_from: datetime
    updated_at: datetime
    provenance: ProvenanceRecord = field(default_factory=ProvenanceRecord)
    observation_count: int = 1  # how many nightly passes have evidenced this
    status: DimensionStatus = DimensionStatus.UNCONFIRMED
    surfaced_in_session: str | None = None  # last session we soft-confirmed it in
    source_session_id: str | None = None  # session a confirm/correct was stated in, if any
    last_observed_at: datetime | None = None  # last nightly pass that (re)saw it
    id: str = field(default_factory=lambda: uuid4().hex)


class SocialEventKind(StrEnum):
    """What kind of matchmaking event a service recorded to the brain (Phase 8 write-back).

    So the companion stays coherent about its own matchmaking — it can later reference that it
    introduced someone / a meet is set / how it went, and jobs for symmetry. Every event is
    stored PER USER and describes the counterpart ONLY anonymously ("someone who loves pottery"),
    never a name — the same privacy bar as the people-match opener.
    """

    PEOPLE_INTRODUCED = "people_introduced"  # surfaced an intro to the user
    PEOPLE_ACCEPTED = "people_accepted"  # user was open to meeting them
    MEET_SET = "meet_set"  # a rendezvous is arranged (rough where/when)
    MET = "met"  # the meet happened (follow-up recorded; the feeling is a separate signal)
    JOB_RECOMMENDED = "job_recommended"  # matching nudged a role (symmetry with people events)
    JOB_LIKED = "job_liked"  # user liked/loved a recommended role


@dataclass(frozen=True, slots=True)
class SocialEvent:
    """A durable, per-user record of alik's matchmaking (people or jobs), for conversational
    continuity. ``summary`` is the anonymized human sentence the companion can recall;
    ``counterpart_ref`` is an OPAQUE id of the other party (never a name) used by the
    cross-service deletion/tombstone path — nullable for job events. Erased by Memory.delete."""

    user_id: str
    kind: SocialEventKind
    summary: str  # anonymized, e.g. "alik introduced them to someone who loves pottery"
    source: str  # which service recorded it: "connections" | "rendezvous" | "matching"
    counterpart_ref: str | None = None  # opaque id of the other person (never their name)
    created_at: datetime | None = None
    id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(frozen=True, slots=True)
class AssembledProfile:
    """The single rich picture of a user, assembled on read (the Profile API payload).

    Combines identity from the auth microservice with everything the brain has learned
    silently: current facts, CONFIRMED traits, and the behavioral dimensions. This is
    what the (next) matching service consumes. ``identity`` is None if auth is down.
    """

    user_id: str
    identity: dict | None
    facts: list[GraphNode] = field(default_factory=list)
    confirmed_traits: list[InferredTrait] = field(default_factory=list)
    dimensions: list[ProfileDimension] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """What a single extraction pass pulled out of one session transcript."""

    facts: list[GraphNode]
    signals: list[GraphNode]
    commitments: list[CommitmentNode]


@dataclass(frozen=True, slots=True)
class RetrievedContext:
    episodes: list[MemoryRecord]  # recent cross-session summaries, oldest -> newest
    working: list[MemoryRecord]  # current session's live turns, in order
    facts: list[GraphNode] = field(default_factory=list)  # current graph facts (valid now)
    commitments: list[CommitmentNode] = field(default_factory=list)  # open commitments (Phase 5)
    reflection: str | None = None  # Phase 3: replaces episodes for 30+ day users
    traits: list[InferredTrait] = field(default_factory=list)  # Phase 4: current inferred traits
    dimensions: list[ProfileDimension] = field(
        default_factory=list
    )  # living-profile behavioral layer
