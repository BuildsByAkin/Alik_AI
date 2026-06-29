"""The living-profile domain: a fixed taxonomy of behavioral dimensions, pure logic.

The profile is a structured BEHAVIORAL layer that sits alongside the free-form
InferredTrait layer. Where a trait is "anything notable about this person" in free
text, a ``ProfileDimension`` places the person on a KNOWN axis (a fixed vocabulary of
values) that we can both adjust the companion's behavior on and hand to matching.

Everything here is pure (no DB, no model, no network) so the accumulation policy and
the behavior mapping are fully testable. The nightly ``profile_pass`` and the stores
call into this module; nothing here imports them.

Confirmed vs unconfirmed is tracked on the dimension's ``status``:
- INFERRED evidence accumulates silently into UNCONFIRMED dimensions.
- When one is confident enough, the companion gently surfaces it in conversation
  (soft-confirm); the user's reply moves it to CONFIRMED or CORRECTED.
- Only CONFIRMED (or sufficiently-confident UNCONFIRMED) dimensions adjust behavior;
  CORRECTED ones never do and are never re-surfaced.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from alik.models import DimensionStatus, ProfileDimension, ProvenanceRecord


class _Dim:
    """One axis: its allowed values and the per-value behavior directive (if any).

    A value with no directive still informs matching/continuity — it just doesn't
    change how the companion talks. Directives are phrased as BEHAVIOR, never as a
    label or anything clinical: we adjust how we show up, we never tell the person
    what they are.
    """

    def __init__(self, description: str, values: dict[str, str | None]) -> None:
        self.description = description
        self.values = values


# The fixed taxonomy. Add an axis or a value here; no other code changes are needed
# (detection prompt, validation, and behavior all read from this table).
TAXONOMY: dict[str, _Dim] = {
    "detail_specificity": _Dim(
        "how detailed and concrete they are when they talk about their interests",
        {
            "vague": None,
            "moderate": None,
            "highly_specific": (
                "They communicate in precise, concrete detail — meet them there: be "
                "specific and concrete rather than general or hand-wavy."
            ),
        },
    ),
    "topic_focus": _Dim(
        "whether they go deep on one topic or skim across many",
        {
            "deep_narrow": (
                "They tend to go deep on what they care about — it's welcome to explore "
                "one topic thoroughly with them rather than skipping across many."
            ),
            "balanced": None,
            "broad_shallow": None,
        },
    ),
    "interest_intensity": _Dim(
        "whether their interests feel casual or intense and specific",
        {
            "casual": None,
            "engaged": None,
            "intense_specific": (
                "Their interests run deep and specific — engage with real depth on them "
                "rather than surface-level enthusiasm."
            ),
        },
    ),
    "structure_preference": _Dim(
        "whether they prefer to know the plan in advance or are happy to be spontaneous",
        {
            "flexible": None,
            "mixed": None,
            "needs_structure": (
                "They feel more at ease when they know the plan — offer specifics and a "
                "clear next step rather than open-ended suggestions."
            ),
        },
    ),
    "sensory_sensitivity": _Dim(
        "whether they find loud, busy, or crowded environments overwhelming",
        {
            "low": None,
            "medium": None,
            "high": (
                "When you suggest places or activities, lean toward calm, low-key settings "
                "over loud or crowded ones."
            ),
        },
    ),
    "social_predictability_need": _Dim(
        "how much they need to know who and what is involved before a social situation",
        {
            "low": None,
            "medium": None,
            "high": (
                "Before anything social, naturally share the who / what / when so it feels "
                "predictable and easy to say yes to."
            ),
        },
    ),
}


def is_valid(dimension: str, value: str) -> bool:
    """True only for a known axis + a value in that axis's vocabulary."""
    dim = TAXONOMY.get(dimension)
    return dim is not None and value in dim.values


def taxonomy_prompt_block() -> str:
    """Render the taxonomy for the detection model: each axis, what it means, its values."""
    lines = []
    for name, dim in TAXONOMY.items():
        allowed = " | ".join(dim.values)
        lines.append(f"- {name}: {dim.description}. One of: {allowed}.")
    return "\n".join(lines)


def behavior_directives(
    dimensions: Sequence[ProfileDimension], *, behavior_min_confidence: float
) -> list[str]:
    """The behavior directives implied by a user's current dimensions.

    A directive applies when the dimension is CONFIRMED, or UNCONFIRMED but at/above
    ``behavior_min_confidence`` (acting softly on a strong signal — these only shape
    tone, never assert a label). CORRECTED dimensions never apply.
    """
    out: list[str] = []
    for d in dimensions:
        if d.status is DimensionStatus.CORRECTED:
            continue
        spec = TAXONOMY.get(d.dimension)
        if spec is None:
            continue
        directive = spec.values.get(d.value)
        if not directive:
            continue
        if d.status is DimensionStatus.CONFIRMED or d.confidence >= behavior_min_confidence:
            out.append(directive)
    return out


def _merge_provenance(
    a: ProvenanceRecord, b: ProvenanceRecord, *, cap: int = 20
) -> ProvenanceRecord:
    """Union two provenance records (order-preserving, de-duplicated, capped)."""

    def union(xs: list[str], ys: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for v in (*xs, *ys):
            if v not in seen:
                seen[v] = None
        return list(seen)[:cap]

    return ProvenanceRecord(
        episode_ids=union(a.episode_ids, b.episode_ids),
        signal_ids=union(a.signal_ids, b.signal_ids),
    )


def apply_observation(
    existing: ProfileDimension | None,
    observed: ProfileDimension,
    *,
    step: float,
    now: datetime,
) -> ProfileDimension:
    """Fold a nightly observation into an UNCONFIRMED dimension's running picture.

    This is the "accumulate quietly over time" rule and is only ever applied to a new
    or UNCONFIRMED dimension (the caller leaves CONFIRMED/CORRECTED ones alone):

    - first sighting -> start at the observed value/confidence.
    - same value again -> raise confidence with diminishing returns; bump the count;
      union the provenance; refresh the human-readable content.
    - a competing value -> if the new evidence is more confident, switch to it (reset
      the count); otherwise keep the current value but decay its confidence a little.

    Single-row-per-axis is a deliberate simplification (we keep the dominant value, not
    a full per-value tally) — good enough to paint a stable picture and self-correct.
    """
    if existing is None:
        return replace(
            observed,
            confidence=_clamp(observed.confidence),
            observation_count=1,
            status=DimensionStatus.UNCONFIRMED,
            valid_from=observed.valid_from or now,
            last_observed_at=now,
            updated_at=now,
        )

    if existing.value == observed.value:
        confidence = existing.confidence + (1.0 - existing.confidence) * step
        return replace(
            existing,
            confidence=_clamp(confidence),
            content=observed.content or existing.content,
            observation_count=existing.observation_count + 1,
            provenance=_merge_provenance(existing.provenance, observed.provenance),
            last_observed_at=now,
            updated_at=now,
        )

    if observed.confidence > existing.confidence:
        return replace(
            existing,
            value=observed.value,
            content=observed.content or existing.content,
            confidence=_clamp(observed.confidence),
            observation_count=1,
            provenance=observed.provenance,
            last_observed_at=now,
            updated_at=now,
        )

    return replace(
        existing,
        confidence=_clamp(existing.confidence * (1.0 - step)),
        observation_count=existing.observation_count + 1,
        last_observed_at=now,
        updated_at=now,
    )


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))
