"""Fact→interest extraction: each mapped key, unmappable handling, weights, dedup."""

from __future__ import annotations

from connections_service.interests import all_interest_nodes, extract_interests
from tests.conftest import dim, make_profile

INTENSE = [dim("interest_intensity", "intense_specific", 0.9)]


def _edges(profile) -> dict[str, float]:
    return {e.interest_node_id: e.weight for e in extract_interests(profile)}


def test_primary_hobby_maps_specific_with_intensity_times_rank():
    e = _edges(
        make_profile(facts={"primary_hobby": "rock climbing on weekends"}, dimensions=INTENSE)
    )
    assert e["outdoor_active:rock_climbing"] == 1.0  # intensity 1.0 * primary rank 1.0


def test_secondary_and_tertiary_rank_scale_weight():
    e = _edges(
        make_profile(
            facts={"secondary_hobby": "hiking", "tertiary_hobby": "baking bread"},
            dimensions=INTENSE,
        )
    )
    assert e["outdoor_active:hiking"] == 0.8  # 1.0 * secondary 0.8
    assert e["food_drink:baking"] == 0.6  # 1.0 * tertiary 0.6


def test_intensity_missing_defaults_to_half():
    e = _edges(make_profile(facts={"primary_hobby": "chess"}))  # no interest_intensity dim
    assert e["intellectual:chess"] == 0.5  # 0.5 * 1.0


def test_unmappable_hobby_yields_no_edge_but_keeps_user():
    edges = extract_interests(make_profile(facts={"primary_hobby": "competitive napping"}))
    assert edges == []  # warned + skipped, no crash


def test_primary_exercise_falls_back_to_wellness_general():
    e = _edges(make_profile(facts={"primary_exercise": "pickleball league"}, dimensions=INTENSE))
    assert "wellness:_general" in e


def test_primary_exercise_maps_specific_when_known():
    e = _edges(make_profile(facts={"primary_exercise": "lifting at the gym"}))
    assert "wellness:strength_training" in e


def test_music_book_sports_category_targeted():
    e = _edges(
        make_profile(
            facts={
                "music_taste": "mostly jazz and folk",
                "book_genre": "sci-fi and fantasy novels",
                "sports_team": "Minnesota Vikings",
            }
        )
    )
    assert e["music_listening:jazz"] == 0.6
    assert e["intellectual:reading_fiction"] == 0.6
    assert e["sports_watching:football"] == 0.5


def test_music_unmappable_genre_falls_back_to_general():
    e = _edges(make_profile(facts={"music_taste": "polka and zydeco"}))
    assert "music_listening:_general" in e


def test_values_core_only_yields_cause_when_matching():
    assert "social_causes:environment" in _edges(
        make_profile(facts={"values_core": "I care a lot about climate"})
    )
    assert _edges(make_profile(facts={"values_core": "kindness and honesty"})) == {}


def test_confirmed_trait_extraction_respects_floor():
    strong = make_profile(
        traits=[{"key": "t1", "content": "loves rock climbing", "confidence": 0.8}]
    )
    assert "outdoor_active:rock_climbing" in _edges(strong)
    weak = make_profile(traits=[{"key": "t1", "content": "loves rock climbing", "confidence": 0.6}])
    assert _edges(weak) == {}


def test_duplicate_node_keeps_max_weight():
    # primary_hobby running (1.0) and primary_exercise running (1.0) → single edge, max weight.
    e = extract_interests(
        make_profile(
            facts={"primary_hobby": "running", "primary_exercise": "running"}, dimensions=INTENSE
        )
    )
    running = [edge for edge in e if edge.interest_node_id == "outdoor_active:running"]
    assert len(running) == 1
    assert running[0].weight == 1.0


def test_taxonomy_has_general_node_per_category():
    ids = {n.id for n in all_interest_nodes()}
    assert "outdoor_active:_general" in ids
    assert "music_listening:_general" in ids
