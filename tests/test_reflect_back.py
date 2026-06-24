"""Reflect-back logic in the Companion, proven infra-free against the doubles.

Covers: surfacing fires only after the 3rd completed turn and at most once per
session; confirm bumps confidence + sets status; correct closes the old trait and
opens a new confirmed one (inheriting provenance); deflect leaves the trait alone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from alik.companion import Companion
from alik.memory.graph import GraphMemory
from alik.models import InferredTrait, ProvenanceRecord, TraitStatus
from alik.prompt import REFLECT_BACK_SYSTEM, RESPONSE_CLASSIFY_SYSTEM, load_persona
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class FakeRBLLM:
    """Phrases reflect-back questions and returns a scripted classification."""

    def __init__(self, classification: str = "confirm", correction: str | None = None) -> None:
        self.classification = classification
        self.correction = correction
        self.reflect_calls = 0
        self.classify_calls = 0

    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        yield "ok "

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        if system == REFLECT_BACK_SYSTEM:
            self.reflect_calls += 1
            return "You seem to light up about your sister — is that right?"
        if system == RESPONSE_CLASSIFY_SYSTEM:
            self.classify_calls += 1
            if self.classification == "correct":
                return f'{{"classification": "correct", "correction_text": "{self.correction}"}}'
            return f'{{"classification": "{self.classification}", "correction_text": null}}'
        return ""  # summary / other


def _companion(mem: GraphMemory, llm: FakeRBLLM) -> Companion:
    return Companion(memory=mem, llm=llm, persona=load_persona(), episode_limit=10)


async def _drain(stream: AsyncIterator[str]) -> None:
    async for _ in stream:
        pass


def _graph_mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


async def _seed_trait(mem: GraphMemory, user_id: str, *, confidence: float = 0.8) -> InferredTrait:
    now = datetime.now(UTC)
    trait = InferredTrait(
        user_id=user_id,
        key="energized_by_sister",
        content="lights up talking about his sister",
        confidence=confidence,
        valid_from=now,
        status_updated_at=now,
        status=TraitStatus.INFERRED,
        provenance=ProvenanceRecord(episode_ids=["ep-1"], signal_ids=["sig-1"]),
    )
    await mem.write_traits([trait])
    return trait


async def test_reflect_back_fires_only_after_third_turn(user_id):
    mem = _graph_mem()
    llm = FakeRBLLM()
    await _seed_trait(mem, user_id)
    companion = _companion(mem, llm)

    # Turns 1-3: not eligible (fewer than 3 completed turns).
    for i in range(3):
        await _drain(companion.respond(user_id, "S", f"msg {i}"))
        assert llm.reflect_calls == 0
    # Turn 4: surfaces exactly once.
    await _drain(companion.respond(user_id, "S", "msg 3"))
    assert llm.reflect_calls == 1
    assert companion._rb_pending.get("S") is not None

    # Turn 5+ never surfaces again this session (at most once).
    await _drain(companion.respond(user_id, "S", "sure, that's true"))
    await _drain(companion.respond(user_id, "S", "anyway"))
    assert llm.reflect_calls == 1


async def _surface(mem: GraphMemory, companion: Companion, user_id: str) -> None:
    for i in range(4):
        await _drain(companion.respond(user_id, "S", f"msg {i}"))


async def test_reflect_back_cooldown_blocks_then_clears(user_id):
    """After a reflect-back fires, the next 3 sessions are skipped; the 4th can fire again.
    The firing session itself does not count toward the cooldown."""
    mem = _graph_mem()
    llm = FakeRBLLM(classification="deflect")  # deflect leaves the trait inferred & re-eligible
    await _seed_trait(mem, user_id, confidence=0.8)
    companion = _companion(mem, llm)  # default cooldown = 3 sessions

    async def run_session(sid: str) -> bool:
        before = llm.reflect_calls
        for i in range(4):  # 4th turn reaches the reflect-back gate
            await _drain(companion.respond(user_id, sid, f"msg {i}"))
        fired = llm.reflect_calls > before
        await companion.end_session(user_id, sid)
        return fired

    fired = [await run_session(f"S{n}") for n in range(1, 6)]
    assert fired == [True, False, False, False, True]  # fire, skip 3, fire


async def test_confirm_bumps_confidence_and_status(user_id):
    mem = _graph_mem()
    llm = FakeRBLLM(classification="confirm")
    await _seed_trait(mem, user_id, confidence=0.8)
    companion = _companion(mem, llm)

    await _surface(mem, companion, user_id)  # turn 4 surfaces
    await _drain(companion.respond(user_id, "S", "yes, totally"))  # turn 5 = confirm
    assert llm.classify_calls == 1

    current = await mem.get_current_traits(user_id)
    assert len(current) == 1
    assert current[0].status is TraitStatus.CONFIRMED
    assert current[0].confidence == 0.9  # 0.8 + 0.1 bump


async def test_correct_closes_old_and_opens_new(user_id):
    mem = _graph_mem()
    llm = FakeRBLLM(classification="correct", correction="he actually lights up about his dog")
    trait = await _seed_trait(mem, user_id)
    companion = _companion(mem, llm)

    await _surface(mem, companion, user_id)
    await _drain(companion.respond(user_id, "S", "no, it's my dog really"))

    old = await mem.get_trait_by_id(trait.id)
    assert old.status is TraitStatus.CORRECTED
    assert old.valid_until is not None  # window closed

    current = await mem.get_current_traits(user_id)
    assert len(current) == 1
    new = current[0]
    assert new.status is TraitStatus.CONFIRMED
    assert new.content == "he actually lights up about his dog"
    assert new.key == trait.key  # same pattern, corrected
    assert new.confidence == 0.7  # corrected_trait_confidence
    # Provenance inherited from the superseded inference (traceability preserved).
    assert new.provenance.episode_ids == ["ep-1"]
    assert new.provenance.signal_ids == ["sig-1"]
    assert new.source_session_id == "S"


async def test_deflect_leaves_trait_unchanged(user_id):
    mem = _graph_mem()
    llm = FakeRBLLM(classification="deflect")
    trait = await _seed_trait(mem, user_id, confidence=0.8)
    companion = _companion(mem, llm)

    await _surface(mem, companion, user_id)
    await _drain(companion.respond(user_id, "S", "ha, who knows"))

    current = await mem.get_current_traits(user_id)
    assert len(current) == 1
    assert current[0].id == trait.id
    assert current[0].status is TraitStatus.INFERRED
    assert current[0].confidence == 0.8
