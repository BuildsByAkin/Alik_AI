"""The living-profile behavioral layer: accumulation, detection, sleep pass, soft-confirm.

Pure logic and the sleep pass run against the doubles (no infra); the soft-confirm loop
is proven on the Companion exactly like reflect-back. Confirmed vs unconfirmed are tracked
separately, so the AI knows which dimensions still need a gentle check.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from alik import profile
from alik.companion import Companion
from alik.config import Settings
from alik.memory.graph import GraphMemory
from alik.models import (
    DimensionStatus,
    GraphNode,
    NodeType,
    ProfileDimension,
    ProvenanceRecord,
)
from alik.prompt import (
    PROFILE_DETECTION_SYSTEM,
    REFLECT_PROFILE_CONFIRM_SYSTEM,
    RESPONSE_CLASSIFY_SYSTEM,
    load_persona,
    parse_profile_detection,
)
from alik.sleep_pass import profile_pass
from tests.conftest import InMemoryGraphStore, InMemoryMemory


def _dim(
    value: str,
    confidence: float,
    *,
    dimension: str = "structure_preference",
    status: DimensionStatus = DimensionStatus.UNCONFIRMED,
    observation_count: int = 2,
    user_id: str = "u",
) -> ProfileDimension:
    now = datetime.now(UTC)
    return ProfileDimension(
        user_id=user_id,
        dimension=dimension,
        value=value,
        content=f"{dimension} looks like {value}",
        confidence=confidence,
        valid_from=now,
        updated_at=now,
        status=status,
        observation_count=observation_count,
        provenance=ProvenanceRecord(episode_ids=["e1"]),
    )


# --- accumulation policy (pure) --------------------------------------------------------


def test_first_sighting_starts_unconfirmed():
    merged = profile.apply_observation(
        None, _dim("needs_structure", 0.5), step=0.25, now=datetime.now(UTC)
    )
    assert merged.value == "needs_structure"
    assert merged.observation_count == 1
    assert merged.status is DimensionStatus.UNCONFIRMED


def test_same_value_raises_confidence_with_diminishing_returns():
    existing = _dim("needs_structure", 0.5, observation_count=1)
    merged = profile.apply_observation(
        existing, _dim("needs_structure", 0.4), step=0.5, now=datetime.now(UTC)
    )
    assert merged.value == "needs_structure"
    assert merged.confidence == 0.75  # 0.5 + (1-0.5)*0.5
    assert merged.observation_count == 2


def test_competing_value_switches_when_more_confident():
    existing = _dim("flexible", 0.4)
    merged = profile.apply_observation(
        existing, _dim("needs_structure", 0.7), step=0.25, now=datetime.now(UTC)
    )
    assert merged.value == "needs_structure"
    assert merged.observation_count == 1


def test_competing_value_decays_when_weaker():
    existing = _dim("flexible", 0.6)
    merged = profile.apply_observation(
        existing, _dim("needs_structure", 0.3), step=0.5, now=datetime.now(UTC)
    )
    assert merged.value == "flexible"
    assert merged.confidence < 0.6


# --- behavior directives (pure) --------------------------------------------------------


def test_behavior_directives_gating():
    dims = [
        _dim("needs_structure", 0.4, status=DimensionStatus.CONFIRMED),  # confirmed -> always
        _dim("high", 0.8, dimension="sensory_sensitivity"),  # unconfirmed >= 0.75 -> in
        _dim("high", 0.5, dimension="social_predictability_need"),  # unconfirmed < 0.75 -> out
        _dim(
            "intense_specific",
            0.99,
            dimension="interest_intensity",
            status=DimensionStatus.CORRECTED,
        ),
    ]
    out = profile.behavior_directives(dims, behavior_min_confidence=0.75)
    assert len(out) == 2
    joined = " ".join(out)
    assert "know the plan" in joined  # structure_preference / needs_structure
    assert "calm, low-key" in joined  # sensory_sensitivity / high


# --- detection parsing -----------------------------------------------------------------


def test_parse_keeps_valid_drops_invalid_and_ungrounded():
    raw = (
        "["
        '{"dimension":"structure_preference","value":"needs_structure","content":"likes a plan",'
        '"confidence":0.7,"provenance_episode_ids":["e1"],"provenance_signal_ids":[]},'
        '{"dimension":"bogus_axis","value":"x","content":"y","confidence":0.9,'
        '"provenance_episode_ids":["e1"]},'
        '{"dimension":"sensory_sensitivity","value":"deafening","content":"z","confidence":0.8,'
        '"provenance_episode_ids":["e1"]},'
        '{"dimension":"interest_intensity","value":"intense_specific","content":"deep on chess",'
        '"confidence":0.8,"provenance_episode_ids":["nope"]}'
        "]"
    )
    dims = parse_profile_detection(
        raw, user_id="u", known_episode_ids={"e1"}, known_signal_ids=set()
    )
    assert [d.dimension for d in dims] == [
        "structure_preference"
    ]  # invalid value + ungrounded dropped


# --- sleep pass ------------------------------------------------------------------------


class _DetectLLM:
    """Returns a profile-detection JSON that cites the first id it sees in the request."""

    def __init__(self, *, dimension: str, value: str) -> None:
        self.dimension = dimension
        self.value = value

    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        yield "ok "

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        if system != PROFILE_DETECTION_SYSTEM:
            return ""
        text = " ".join(m["content"] for m in messages)
        m = re.search(r"\[(?:ep|sig):([^\]]+)\]", text)
        # Cite the real id in both arrays; parse keeps it in whichever set it actually
        # belongs to (episode vs signal) and drops it from the other.
        cid = f'"{m.group(1)}"' if m else ""
        return (
            f'[{{"dimension":"{self.dimension}","value":"{self.value}",'
            f'"content":"observed","confidence":0.7,'
            f'"provenance_episode_ids":[{cid}],"provenance_signal_ids":[{cid}]}}]'
        )


def _graph_mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


async def _seed_signal(mem: GraphMemory, user_id: str) -> None:
    await mem.write_nodes(
        [
            GraphNode(
                user_id=user_id,
                type=NodeType.EMOTIONAL_SIGNAL,
                key="plans",
                content="kept asking for the exact plan before agreeing to meet",
                valid_from=datetime.now(UTC),
            )
        ]
    )


async def test_profile_pass_writes_a_dimension(user_id):
    mem = _graph_mem()
    await _seed_signal(mem, user_id)
    llm = _DetectLLM(dimension="structure_preference", value="needs_structure")

    updated = await profile_pass(mem, llm, user_id, Settings())

    assert updated == 1
    dims = await mem.get_profile_dimensions(user_id)
    assert len(dims) == 1
    assert dims[0].dimension == "structure_preference"
    assert dims[0].value == "needs_structure"
    assert dims[0].status is DimensionStatus.UNCONFIRMED


async def test_profile_pass_corroborates_confirmed_without_clobbering(user_id):
    mem = _graph_mem()
    await _seed_signal(mem, user_id)
    await mem.put_profile_dimension(
        _dim("needs_structure", 0.8, status=DimensionStatus.CONFIRMED, user_id=user_id)
    )
    # Detection now claims a DIFFERENT value for the same confirmed axis.
    llm = _DetectLLM(dimension="structure_preference", value="flexible")

    await profile_pass(mem, llm, user_id, Settings())

    dims = await mem.get_profile_dimensions(user_id)
    assert len(dims) == 1
    assert dims[0].status is DimensionStatus.CONFIRMED
    assert dims[0].value == "needs_structure"  # authoritative — not overwritten
    assert dims[0].observation_count == 3  # corroborated (2 -> 3)


async def test_profile_pass_leaves_corrected_untouched(user_id):
    mem = _graph_mem()
    await _seed_signal(mem, user_id)
    await mem.put_profile_dimension(
        _dim("needs_structure", 0.4, status=DimensionStatus.CORRECTED, user_id=user_id)
    )
    llm = _DetectLLM(dimension="structure_preference", value="needs_structure")

    await profile_pass(mem, llm, user_id, Settings())

    dims = await mem.get_profile_dimensions(user_id)
    assert dims[0].status is DimensionStatus.CORRECTED
    assert dims[0].observation_count == 2  # untouched — no nagging


# --- soft-confirm on the Companion -----------------------------------------------------


class _ConfirmLLM:
    def __init__(self, classification: str = "confirm") -> None:
        self.classification = classification
        self.last_system: str | None = None
        self.confirm_calls = 0
        self.classify_calls = 0

    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        self.last_system = system
        yield "ok "

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        self.last_system = system
        if system == REFLECT_PROFILE_CONFIRM_SYSTEM:
            self.confirm_calls += 1
            return "you seem to like knowing the plan ahead — is that you, or am I off?"
        if system == RESPONSE_CLASSIFY_SYSTEM:
            self.classify_calls += 1
            return f'{{"classification": "{self.classification}", "correction_text": null}}'
        return "summary"


def _companion(mem: GraphMemory, llm) -> Companion:
    return Companion(memory=mem, llm=llm, persona=load_persona(), episode_limit=10)


async def _drain(stream: AsyncIterator[str]) -> None:
    async for _ in stream:
        pass


async def _seed_confirmable(mem: GraphMemory, user_id: str) -> None:
    await mem.put_profile_dimension(
        _dim("needs_structure", 0.8, observation_count=3, user_id=user_id)
    )


async def test_soft_confirm_fires_only_after_third_turn(user_id):
    mem = _graph_mem()
    llm = _ConfirmLLM()
    await _seed_confirmable(mem, user_id)
    companion = _companion(mem, llm)

    for i in range(3):
        await _drain(companion.respond(user_id, "S", f"msg {i}"))
        assert llm.confirm_calls == 0
    await _drain(companion.respond(user_id, "S", "msg 3"))
    assert llm.confirm_calls == 1
    assert companion._pd_pending.get("S") == "structure_preference"


async def test_soft_confirm_confirm_sets_confirmed(user_id):
    mem = _graph_mem()
    llm = _ConfirmLLM(classification="confirm")
    await _seed_confirmable(mem, user_id)
    companion = _companion(mem, llm)

    for i in range(4):
        await _drain(companion.respond(user_id, "S", f"msg {i}"))
    await _drain(companion.respond(user_id, "S", "yeah, I really do"))  # reply = confirm

    dims = await mem.get_profile_dimensions(user_id)
    assert dims[0].status is DimensionStatus.CONFIRMED
    assert dims[0].confidence == 0.9  # 0.8 + 0.1 bump


async def test_soft_confirm_correct_sets_corrected(user_id):
    mem = _graph_mem()
    llm = _ConfirmLLM(classification="correct")
    await _seed_confirmable(mem, user_id)
    companion = _companion(mem, llm)

    for i in range(4):
        await _drain(companion.respond(user_id, "S", f"msg {i}"))
    await _drain(companion.respond(user_id, "S", "no, I'm pretty go-with-the-flow"))

    dims = await mem.get_profile_dimensions(user_id)
    assert dims[0].status is DimensionStatus.CORRECTED


async def test_confirmed_dimension_drives_behavior_directive(user_id):
    mem = _graph_mem()
    llm = _ConfirmLLM()
    await mem.put_profile_dimension(
        _dim(
            "high",
            0.5,
            dimension="sensory_sensitivity",
            status=DimensionStatus.CONFIRMED,
            user_id=user_id,
        )
    )
    companion = _companion(mem, llm)

    await _drain(companion.respond(user_id, "S", "hey"))

    assert "calm, low-key" in (llm.last_system or "")


async def test_delete_erases_dimensions(user_id):
    mem = _graph_mem()
    await mem.put_profile_dimension(_dim("needs_structure", 0.8, user_id=user_id))
    await mem.delete(user_id)
    assert await mem.get_profile_dimensions(user_id) == []
