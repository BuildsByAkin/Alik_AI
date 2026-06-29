"""The deterministic compatibility kernel — a PURE function (no store, no LLM, no I/O).

Given two users' already-read interest edges + dimension snapshots, it produces a
``CandidateScore``: a 0-1 score, a structured explanation, a confidence signal (data volume,
independent of the score), and a human_review_flag. Fully testable with no infra.

Score = renormalized weighted sum of three components (weights in Settings):
  interest (weighted Jaccard over specific nodes, broad-category fallback when none shared),
  dimensions (per-axis mixed similarity/compatibility), values (social_causes Jaccard).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from connections_service.config import Settings
from connections_service.models import (
    CandidateScore,
    DimensionMatch,
    DimensionSnapshot,
    InterestEdge,
    InterestMatch,
    KernelExplanation,
    MatchType,
)

# --- the behavioral compatibility matrices (product decisions; reviewed before hardcoding) ---

# SIMILARITY axes: same value best. order matters; score by index distance.
_SIM_ORDER: dict[str, list[str]] = {
    "topic_focus": ["deep_narrow", "balanced", "broad_shallow"],
    "interest_intensity": ["casual", "engaged", "intense_specific"],
}
_SIM_BY_DISTANCE = {0: 1.0, 1: 0.6, 2: 0.2}

# COMPATIBILITY axes: non-clash matters, not sameness.
# social_predictability_need and sensory_sensitivity share this matrix.
_PREDICTABILITY = {
    "low": {"low": 1.0, "medium": 0.7, "high": 0.3},
    "medium": {"low": 0.7, "medium": 0.7, "high": 0.7},
    "high": {"low": 0.3, "medium": 0.7, "high": 1.0},
}
_STRUCTURE = {
    "flexible": {"flexible": 1.0, "mixed": 0.8, "needs_structure": 0.5},
    "mixed": {"flexible": 0.8, "mixed": 1.0, "needs_structure": 0.8},
    "needs_structure": {"flexible": 0.5, "mixed": 0.8, "needs_structure": 1.0},
}
_COMPAT_MATRICES = {
    "social_predictability_need": _PREDICTABILITY,
    "sensory_sensitivity": _PREDICTABILITY,
    "structure_preference": _STRUCTURE,
}

# The 5 axes the kernel scores. detail_specificity is intentionally excluded (it's about how
# someone communicates, not who they click with).
SCORED_AXES = (
    "topic_focus",
    "interest_intensity",
    "social_predictability_need",
    "sensory_sensitivity",
    "structure_preference",
)
_N_SCORED_AXES = len(SCORED_AXES)
_GENERAL_SUFFIX = ":_general"


@dataclass(frozen=True, slots=True)
class MatchInput:
    """The store-read data the kernel scores (already ingested — no brain/LLM calls)."""

    user_id: str
    interests: Sequence[InterestEdge]
    dimensions: Sequence[DimensionSnapshot]


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


# --- interest component -----------------------------------------------------------------


def _specific_weights(edges: Sequence[InterestEdge]) -> dict[str, float]:
    """node_id -> weight, EXCLUDING the per-category ``_general`` catch-all nodes."""
    return {
        e.interest_node_id: e.weight
        for e in edges
        if not e.interest_node_id.endswith(_GENERAL_SUFFIX)
    }


def _broad_categories(edges: Sequence[InterestEdge]) -> set[str]:
    """Every edge's broad category (incl. ``_general``) — node id is ``"{broad}:{specific}"``."""
    return {e.interest_node_id.split(":", 1)[0] for e in edges}


def _weighted_jaccard(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    if not keys:
        return 0.0
    num = sum(min(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    den = sum(max(a.get(k, 0.0), b.get(k, 0.0)) for k in keys)
    return num / den if den else 0.0


def _binary_jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _interest_component(
    a: Sequence[InterestEdge], b: Sequence[InterestEdge], broad_multiplier: float
) -> tuple[float, list[InterestMatch], list[str], MatchType]:
    # values_core-derived (social_causes) edges are scored ONLY in the values component, so a
    # shared cause does not double-count here — and a values-only overlap stays "values_only".
    a_i = [e for e in a if e.source_fact_key != "values_core"]
    b_i = [e for e in b if e.source_fact_key != "values_core"]
    a_spec, b_spec = _specific_weights(a_i), _specific_weights(b_i)
    a_broad, b_broad = _broad_categories(a_i), _broad_categories(b_i)
    broad_shared = sorted(a_broad & b_broad)

    specific_j = _weighted_jaccard(a_spec, b_spec)
    if specific_j > 0:
        matches = [
            InterestMatch(
                node_id=nid,
                broad_category=nid.split(":", 1)[0],
                specific_interest=nid.split(":", 1)[1],
                weight_a=a_spec[nid],
                weight_b=b_spec[nid],
            )
            for nid in sorted(set(a_spec) & set(b_spec))
        ]
        return specific_j, matches, broad_shared, "specific"

    broad_j = _binary_jaccard(a_broad, b_broad)
    if broad_j > 0:
        return broad_j * broad_multiplier, [], broad_shared, "broad_only"
    return 0.0, [], broad_shared, "none"  # finalized to values_only/none by the caller


# --- dimension component ----------------------------------------------------------------


def _axis_score(axis: str, value_a: str, value_b: str) -> float | None:
    if axis in _SIM_ORDER:
        order = _SIM_ORDER[axis]
        if value_a not in order or value_b not in order:
            return None
        return _SIM_BY_DISTANCE[abs(order.index(value_a) - order.index(value_b))]
    matrix = _COMPAT_MATRICES.get(axis)
    if matrix is None:
        return None
    return (matrix.get(value_a) or {}).get(value_b)


def _dimension_component(
    a: Sequence[DimensionSnapshot], b: Sequence[DimensionSnapshot], floor: float
) -> tuple[float | None, list[DimensionMatch]]:
    a_by = {d.dimension: d for d in a}
    b_by = {d.dimension: d for d in b}
    matches: list[DimensionMatch] = []
    for axis in SCORED_AXES:
        da, db = a_by.get(axis), b_by.get(axis)
        if da is None or db is None:  # never penalize missing data
            continue
        if da.confidence < floor or db.confidence < floor:
            continue
        score = _axis_score(axis, da.value, db.value)
        if score is None:  # unrecognized value
            continue
        mode = "similarity" if axis in _SIM_ORDER else "compatibility"
        matches.append(DimensionMatch(axis, da.value, db.value, score, mode))
    if not matches:
        return None, []
    return sum(m.axis_score for m in matches) / len(matches), matches


# --- values component -------------------------------------------------------------------


def _values_nodes(edges: Sequence[InterestEdge]) -> set[str]:
    return {e.interest_node_id for e in edges if e.source_fact_key == "values_core"}


def _values_component(
    a: Sequence[InterestEdge], b: Sequence[InterestEdge]
) -> tuple[float | None, list[str]]:
    a_v, b_v = _values_nodes(a), _values_nodes(b)
    if not a_v and not b_v:  # skip only if NEITHER has any cause
        return None, []
    return _binary_jaccard(a_v, b_v), sorted(a_v & b_v)


# --- confidence -------------------------------------------------------------------------


def _confidence(
    a: MatchInput,
    b: MatchInput,
    present_axes: int,
    has_specific: bool,
    has_broad: bool,
    settings: Settings,
) -> float:
    target = settings.confidence_target_edges
    weaker = min(len(a.interests), len(b.interests))
    interest_evidence = _clamp(weaker / target) if target > 0 else 0.0
    dimension_evidence = present_axes / _N_SCORED_AXES
    match_specificity = 1.0 if has_specific else (0.5 if has_broad else 0.2)
    return _clamp(0.5 * interest_evidence + 0.3 * dimension_evidence + 0.2 * match_specificity)


# --- the kernel -------------------------------------------------------------------------


def kernel(a: MatchInput, b: MatchInput, settings: Settings) -> CandidateScore:
    interest_score, specific_matches, broad_shared, i_type = _interest_component(
        a.interests, b.interests, settings.broad_interest_multiplier
    )
    dimension_score, dimension_matches = _dimension_component(
        a.dimensions, b.dimensions, settings.dimension_axis_confidence_floor
    )
    values_score, shared_causes = _values_component(a.interests, b.interests)

    # Renormalize over PRESENT components (interest is always present).
    components = [(settings.interest_weight, interest_score)]
    if dimension_score is not None:
        components.append((settings.dimension_weight, dimension_score))
    if values_score is not None:
        components.append((settings.values_weight, values_score))
    total_weight = sum(w for w, _ in components)
    score = sum(w * s for w, s in components) / total_weight if total_weight else 0.0

    # match_type: interest result wins; else values_only if causes shared; else none.
    if i_type in ("specific", "broad_only"):
        match_type: MatchType = i_type
    elif shared_causes:
        match_type = "values_only"
    else:
        match_type = "none"

    confidence = _confidence(
        a, b, len(dimension_matches), bool(specific_matches), bool(broad_shared), settings
    )

    return CandidateScore(
        user_id_a=a.user_id,
        user_id_b=b.user_id,
        score=round(score, 4),
        interest_score=round(interest_score, 4),
        dimension_score=round(dimension_score or 0.0, 4),
        values_score=round(values_score or 0.0, 4),
        confidence=round(confidence, 4),
        human_review_flag=confidence < settings.confidence_review_threshold,
        explanation=KernelExplanation(
            interest_specific=specific_matches,
            interest_broad=broad_shared,
            dimensions=dimension_matches,
            values_causes=shared_causes,
            match_type=match_type,
        ),
    )
