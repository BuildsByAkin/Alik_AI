"""Semantic interest tagging: request building, response parsing (canonical + guarded), the
LLM call's graceful fallback, and the ingest wiring that prefers tagging but degrades to keyword."""

from __future__ import annotations

import pytest

from connections_service.ingest import _derive_interests
from connections_service.interest_tagger import (
    build_tag_request,
    parse_tags,
    tag_interests,
)
from tests.conftest import make_profile


class FakeLLM:
    def __init__(self, reply: str = "", *, raises: bool = False) -> None:
        self.reply = reply
        self.raises = raises
        self.last_system: str | None = None

    async def complete(self, *, system: str, messages) -> str:
        if self.raises:
            raise RuntimeError("api down")
        self.last_system = system
        return self.reply


def _edges(edges) -> dict[str, float]:
    return {e.interest_node_id: e.weight for e in edges}


# --- request building ---------------------------------------------------------


def test_build_request_gathers_interest_facts_and_confirmed_traits():
    profile = make_profile(
        facts={"primary_hobby": "pottery", "health_concern": "anxiety"},
        traits=[{"key": "t", "content": "obsessed with folk music", "confidence": 0.9}],
    )
    msgs = build_tag_request(profile, trait_confidence_floor=0.7)
    body = msgs[0]["content"]
    assert "pottery" in body
    assert "folk music" in body
    assert "anxiety" not in body  # sensitive key never sent


def test_build_request_none_when_no_interest_signals():
    assert build_tag_request(make_profile(facts={}), trait_confidence_floor=0.7) is None


def test_low_confidence_traits_excluded():
    profile = make_profile(traits=[{"key": "t", "content": "likes chess", "confidence": 0.5}])
    assert build_tag_request(profile, trait_confidence_floor=0.7) is None


# --- response parsing ---------------------------------------------------------


def test_parse_maps_intensity_to_weight():
    raw = '{"interests": [{"node": "outdoor_active:hiking", "intensity": "intense"}]}'
    assert _edges(parse_tags(raw)) == {"outdoor_active:hiking": 1.0}
    raw = '{"interests": [{"node": "creative:pottery", "intensity": "casual"}]}'
    assert _edges(parse_tags(raw)) == {"creative:pottery": 0.4}


def test_parse_defaults_weight_when_intensity_missing():
    raw = '{"interests": [{"node": "gaming:dnd"}]}'
    assert _edges(parse_tags(raw)) == {"gaming:dnd": 0.7}


def test_parse_drops_hallucinated_nodes_keeps_valid():
    raw = '{"interests": [{"node": "made_up:thing"}, {"node": "creative:pottery"}]}'
    assert _edges(parse_tags(raw)) == {"creative:pottery": 0.7}


def test_parse_accepts_general_fallback_node():
    raw = '{"interests": [{"node": "outdoor_active:_general", "intensity": "engaged"}]}'
    assert "outdoor_active:_general" in _edges(parse_tags(raw))


def test_parse_dedups_keeping_max_weight():
    raw = (
        '{"interests": ['
        '{"node": "creative:pottery", "intensity": "casual"},'
        '{"node": "creative:pottery", "intensity": "intense"}]}'
    )
    assert _edges(parse_tags(raw)) == {"creative:pottery": 1.0}


def test_parse_empty_list_is_valid_empty():
    assert parse_tags('{"interests": []}') == []


def test_parse_malformed_returns_none():
    assert parse_tags("not json at all") is None
    assert parse_tags('{"nope": 1}') is None


# --- tag_interests (the LLM call + fallback signalling) -----------------------


async def test_tag_interests_returns_edges_on_good_reply():
    profile = make_profile(facts={"primary_hobby": "throwing clay on a wheel"})
    llm = FakeLLM('{"interests": [{"node": "creative:pottery", "intensity": "intense"}]}')
    edges = await tag_interests(llm, profile, trait_confidence_floor=0.7)
    assert _edges(edges) == {"creative:pottery": 1.0}
    assert "Catalog" in llm.last_system  # the taxonomy was sent


async def test_tag_interests_none_on_llm_error():
    profile = make_profile(facts={"primary_hobby": "pottery"})
    assert await tag_interests(FakeLLM(raises=True), profile, trait_confidence_floor=0.7) is None


async def test_tag_interests_none_on_no_signals():
    assert (
        await tag_interests(FakeLLM("{}"), make_profile(facts={}), trait_confidence_floor=0.7)
        is None
    )


# --- ingest wiring: prefer tagging, fall back to keyword ----------------------


async def test_derive_prefers_llm_when_available(monkeypatch):
    from connections_service.config import settings

    monkeypatch.setattr(settings, "interest_tagging_enabled", True)
    profile = make_profile(facts={"primary_hobby": "scrambling up rock faces"})  # no keyword hit
    llm = FakeLLM(
        '{"interests": [{"node": "outdoor_active:rock_climbing", "intensity": "engaged"}]}'
    )
    edges = await _derive_interests(profile, settings, llm)
    assert "outdoor_active:rock_climbing" in _edges(edges)  # semantic win — keyword would miss


async def test_derive_falls_back_to_keyword_when_llm_fails(monkeypatch):
    from connections_service.config import settings

    monkeypatch.setattr(settings, "interest_tagging_enabled", True)
    profile = make_profile(facts={"primary_hobby": "rock climbing"})  # keyword-mappable
    edges = await _derive_interests(profile, settings, FakeLLM(raises=True))
    assert "outdoor_active:rock_climbing" in _edges(edges)  # keyword path caught it


async def test_derive_uses_keyword_when_llm_none(monkeypatch):
    from connections_service.config import settings

    monkeypatch.setattr(settings, "interest_tagging_enabled", True)
    profile = make_profile(facts={"primary_hobby": "pottery"})
    edges = await _derive_interests(profile, settings, None)
    assert "creative:pottery" in _edges(edges)


@pytest.mark.parametrize("enabled", [False])
async def test_derive_skips_llm_when_disabled(monkeypatch, enabled):
    from connections_service.config import settings

    monkeypatch.setattr(settings, "interest_tagging_enabled", enabled)
    profile = make_profile(facts={"primary_hobby": "pottery"})
    # LLM would return climbing, but tagging is disabled -> keyword path yields pottery.
    llm = FakeLLM('{"interests": [{"node": "outdoor_active:rock_climbing"}]}')
    edges = await _derive_interests(profile, settings, llm)
    assert "creative:pottery" in _edges(edges)
    assert "outdoor_active:rock_climbing" not in _edges(edges)
