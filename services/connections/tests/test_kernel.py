"""The pure compatibility kernel: weighted Jaccard, broad fallback, every axis mode,
missing-data handling, confidence/flag, values, and the structured explanation."""

from __future__ import annotations

from connections_service.config import Settings
from connections_service.kernel import MatchInput, kernel
from connections_service.models import DimensionSnapshot, InterestEdge

S = Settings()


def edge(node: str, w: float = 1.0, src: str = "primary_hobby") -> InterestEdge:
    return InterestEdge(node, w, src)


def dim(axis: str, value: str, conf: float = 0.9) -> DimensionSnapshot:
    return DimensionSnapshot(axis, value, conf, "confirmed")


def score(a_i=None, b_i=None, a_d=None, b_d=None):
    return kernel(MatchInput("a", a_i or [], a_d or []), MatchInput("b", b_i or [], b_d or []), S)


# --- interest ---------------------------------------------------------------------------


def test_specific_weighted_jaccard():
    cs = score(
        [edge("outdoor_active:running", 1.0), edge("gaming:dnd", 0.5)],
        [edge("outdoor_active:running", 0.5), edge("creative:writing", 0.6)],
    )
    assert cs.interest_score == round(0.5 / 2.1, 4)  # Σmin / Σmax over the union
    assert cs.score == cs.interest_score  # only interest present → renormalized to itself
    assert cs.explanation.match_type == "specific"
    assert [m.node_id for m in cs.explanation.interest_specific] == ["outdoor_active:running"]


def test_broad_fallback_when_no_specific_overlap():
    cs = score([edge("outdoor_active:hiking")], [edge("outdoor_active:running")])
    assert cs.explanation.match_type == "broad_only"
    assert cs.explanation.interest_specific == []
    assert cs.explanation.interest_broad == ["outdoor_active"]
    assert cs.interest_score == round(1.0 * S.broad_interest_multiplier, 4)


def test_general_nodes_excluded_from_specific_only_count_broad():
    cs = score(
        [edge("outdoor_active:_general"), edge("gaming:dnd")],
        [edge("outdoor_active:_general"), edge("gaming:dnd")],
    )
    assert [m.node_id for m in cs.explanation.interest_specific] == ["gaming:dnd"]
    assert set(cs.explanation.interest_broad) == {"outdoor_active", "gaming"}
    assert cs.explanation.match_type == "specific"


def test_no_overlap_is_match_type_none():
    cs = score([edge("gaming:dnd")], [edge("creative:writing")])
    assert cs.explanation.match_type == "none"
    assert cs.interest_score == 0.0


# --- dimensions -------------------------------------------------------------------------


def test_similarity_axis_same_adjacent_opposite():
    assert (
        score(
            a_d=[dim("topic_focus", "balanced")], b_d=[dim("topic_focus", "balanced")]
        ).dimension_score
        == 1.0
    )
    assert (
        score(
            a_d=[dim("topic_focus", "deep_narrow")], b_d=[dim("topic_focus", "balanced")]
        ).dimension_score
        == 0.6
    )
    assert (
        score(
            a_d=[dim("topic_focus", "deep_narrow")], b_d=[dim("topic_focus", "broad_shallow")]
        ).dimension_score
        == 0.2
    )


def test_compatibility_axes_matrices():
    cs = score(
        a_d=[dim("social_predictability_need", "high"), dim("structure_preference", "flexible")],
        b_d=[
            dim("social_predictability_need", "low"),
            dim("structure_preference", "needs_structure"),
        ],
    )
    by = {m.axis: m for m in cs.explanation.dimensions}
    assert by["social_predictability_need"].axis_score == 0.3  # high+low = clash
    assert by["social_predictability_need"].scoring_mode == "compatibility"
    assert by["structure_preference"].axis_score == 0.5  # flexible+needs_structure
    assert cs.dimension_score == round((0.3 + 0.5) / 2, 4)


def test_missing_dimension_is_skipped_not_penalized():
    cs = score(
        [edge("gaming:dnd")], [edge("gaming:dnd")], a_d=[dim("topic_focus", "balanced")], b_d=[]
    )
    assert cs.explanation.dimensions == []
    assert cs.dimension_score == 0.0
    assert cs.score == cs.interest_score  # dims absent → don't drag the score


def test_low_confidence_axis_is_skipped():
    cs = score(a_d=[dim("topic_focus", "balanced", 0.4)], b_d=[dim("topic_focus", "balanced", 0.9)])
    assert cs.explanation.dimensions == []


# --- confidence / flag ------------------------------------------------------------------


def test_thin_profile_flags_for_review():
    cs = score([edge("gaming:dnd")], [edge("gaming:dnd")])  # 1 edge each, no dims
    assert cs.confidence < S.confidence_review_threshold
    assert cs.human_review_flag is True


def test_rich_profile_is_not_flagged():
    interests = [edge("gaming:dnd"), edge("outdoor_active:running"), edge("creative:writing")]
    dims = [
        dim("topic_focus", "balanced"),
        dim("interest_intensity", "engaged"),
        dim("structure_preference", "mixed"),
    ]
    cs = score(interests, interests, dims, dims)
    assert cs.human_review_flag is False


# --- values -----------------------------------------------------------------------------


def test_values_shared_cause_scores_and_is_not_double_counted():
    cause = edge("social_causes:environment", src="values_core")
    cs = score([edge("gaming:dnd"), cause], [edge("gaming:dnd"), cause])
    assert cs.values_score == 1.0
    assert cs.explanation.values_causes == ["social_causes:environment"]
    assert [m.node_id for m in cs.explanation.interest_specific] == ["gaming:dnd"]  # cause not here


def test_values_only_match_type():
    cause = edge("social_causes:environment", src="values_core")
    cs = score([cause], [cause])
    assert cs.explanation.match_type == "values_only"
    assert cs.interest_score == 0.0
    assert cs.values_score == 1.0


def test_values_skipped_when_neither_has_causes():
    cs = score([edge("gaming:dnd")], [edge("gaming:dnd")])
    assert cs.values_score == 0.0
    assert cs.explanation.values_causes == []
    assert cs.score == 1.0  # interest 1.0, values skipped → not capped


def test_values_one_sided_scores_zero():
    cause = edge("social_causes:environment", src="values_core")
    cs = score([edge("gaming:dnd"), cause], [edge("gaming:dnd")])
    assert cs.values_score == 0.0  # a has a cause, b doesn't → Jaccard 0, counted (not skipped)


# --- combination ------------------------------------------------------------------------


def test_full_score_renormalizes_over_present_components():
    cs = score(
        [edge("gaming:dnd")],
        [edge("gaming:dnd")],
        a_d=[dim("topic_focus", "balanced")],
        b_d=[dim("topic_focus", "balanced")],
    )
    # interest 1.0 (w .5) + dimension 1.0 (w .35), no values → renormalized to 1.0
    assert cs.score == 1.0
