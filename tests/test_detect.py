"""detect() (the fifth sleep pass) over the in-memory double.

Proves: detection produces InferredTraits carrying provenance; an ungrounded
inference (no cited ids) is rejected; temporal resolution supersedes on a
duplicate key with new content, and is a no-op on identical content.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

from alik import sleep_pass
from alik.config import Settings
from alik.memory.graph import GraphMemory
from alik.models import (
    GraphNode,
    InferredTrait,
    MemoryRecord,
    MemoryTier,
    NodeType,
    ProvenanceRecord,
    TraitStatus,
)
from alik.prompt import (
    CONSOLIDATE_SYSTEM,
    DETECTION_SYSTEM,
    build_detection_request,
    parse_detection,
)
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class FakeDetectLLM:
    """Returns a scripted detection array, echoing back ids it finds in the prompt."""

    def __init__(self, content: str = "lights up talking about his sister") -> None:
        self.content = content

    async def stream_reply(
        self, *, system: str, messages: Sequence[dict]
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        assert system == DETECTION_SYSTEM
        text = messages[0]["content"]
        # Pull the first episode + signal id the request advertised, to cite as provenance.
        ep_ids = [tok[4:-1] for tok in text.split() if tok.startswith("[ep:")]
        sig_ids = [tok[5:-1] for tok in text.split() if tok.startswith("[sig:")]
        item = {
            "key": "energized_by_sister",
            "content": self.content,
            "confidence": 0.8,
            "provenance_episode_ids": ep_ids[:1],
            "provenance_signal_ids": sig_ids[:1],
        }
        # A second, ungrounded pattern that must be dropped (no valid provenance).
        ungrounded = {
            "key": "made_up",
            "content": "invented pattern",
            "confidence": 0.9,
            "provenance_episode_ids": ["does-not-exist"],
            "provenance_signal_ids": [],
        }
        return json.dumps([item, ungrounded])


async def _seed(mem: GraphMemory, user_id: str) -> str:
    await mem.write(
        MemoryRecord(
            user_id=user_id,
            session_id="s",
            tier=MemoryTier.EPISODIC,
            content="talked warmly about his sister visiting",
        )
    )
    # Promote it so detect() sees it (detect reads promoted episodes).
    eps = await mem.get_recent_episodes(user_id)
    await mem.promote_episode(eps[0].id)
    await mem.write_nodes(
        [
            GraphNode(
                user_id=user_id,
                type=NodeType.EMOTIONAL_SIGNAL,
                key="energy_source",
                content="happy when family comes up",
                valid_from=eps[0].created_at,
            )
        ]
    )
    return eps[0].id


async def test_detect_produces_traits_with_provenance(user_id):
    mem = GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)
    await _seed(mem, user_id)

    traits = await sleep_pass.detect(mem, FakeDetectLLM(), user_id, Settings())

    # Only the grounded trait survives; the ungrounded one is dropped.
    assert len(traits) == 1
    t = traits[0]
    assert t.key == "energized_by_sister"
    assert t.status is TraitStatus.INFERRED
    assert t.provenance.episode_ids, "episode provenance is mandatory and must be cited"
    assert t.provenance.signal_ids, "signal provenance must be cited"

    stored = await mem.get_current_traits(user_id)
    assert {s.key for s in stored} == {"energized_by_sister"}


async def test_detect_dedups_reworded_or_repeated_same_key(user_id):
    """Same key + similar/identical content is a RE-DETECTION, not a new node: no churn,
    same id kept (Phase 5.2 — what keeps the inferred layer from exploding)."""
    mem = GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)
    await _seed(mem, user_id)

    await sleep_pass.detect(mem, FakeDetectLLM("lights up about his sister"), user_id, Settings())
    first = (await mem.get_current_traits(user_id))[0]

    # Reworded same pattern → treated as re-detection: still one node, content unchurned.
    await sleep_pass.detect(
        mem, FakeDetectLLM("comes alive talking about his sister"), user_id, Settings()
    )
    current = await mem.get_current_traits(user_id)
    assert len(current) == 1
    assert current[0].id == first.id
    assert current[0].content == "lights up about his sister"  # not churned

    # Exact repeat → also a no-op.
    await sleep_pass.detect(mem, FakeDetectLLM("lights up about his sister"), user_id, Settings())
    assert len(await mem.get_current_traits(user_id)) == 1


async def test_detect_supersedes_on_clearly_different_content(user_id):
    """Same key but genuinely different content still supersedes (the pattern changed)."""
    mem = GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)
    await _seed(mem, user_id)

    await sleep_pass.detect(mem, FakeDetectLLM("lights up about his sister"), user_id, Settings())
    await sleep_pass.detect(
        mem, FakeDetectLLM("avoids the topic of family entirely"), user_id, Settings()
    )
    current = await mem.get_current_traits(user_id)
    assert len(current) == 1
    assert current[0].content == "avoids the topic of family entirely"


async def test_detect_never_clobbers_a_confirmed_trait(user_id):
    """A user-confirmed trait is authoritative — re-detection with new wording on the
    same key must NOT supersede it (protects confirmations; deterministic idempotency)."""
    mem = GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)
    await _seed(mem, user_id)

    traits = await sleep_pass.detect(
        mem, FakeDetectLLM("lights up about his sister"), user_id, Settings()
    )
    confirmed_id = traits[0].id
    await mem.confirm_trait(confirmed_id, confidence_bump=0.1)

    # A later sleep pass re-detects the same key with DIFFERENT wording.
    await sleep_pass.detect(
        mem, FakeDetectLLM("totally different phrasing about his sister"), user_id, Settings()
    )

    current = await mem.get_current_traits(user_id)
    assert len(current) == 1
    assert current[0].id == confirmed_id  # untouched
    assert current[0].status is TraitStatus.CONFIRMED
    assert current[0].content == "lights up about his sister"  # not overwritten


async def test_prune_closes_stale_inferred_but_keeps_confirmed(user_id):
    """Inferred traits not re-detected within the staleness window get closed; confirmed
    traits of the same age are never pruned; recently-detected inferred traits survive."""
    mem = GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)
    now = datetime.now(UTC)
    old = now - timedelta(days=15)  # older than the 14-day window

    def trait(key, *, status, last_detected):
        return InferredTrait(
            user_id=user_id,
            key=key,
            content=key,
            confidence=0.8,
            valid_from=old,
            status_updated_at=old,
            status=status,
            provenance=ProvenanceRecord(episode_ids=["e1"]),
            last_detected_at=last_detected,
        )

    await mem._graph.insert_trait(
        trait("stale_inferred", status=TraitStatus.INFERRED, last_detected=old)
    )
    await mem._graph.insert_trait(
        trait("old_confirmed", status=TraitStatus.CONFIRMED, last_detected=old)
    )
    await mem._graph.insert_trait(
        trait("fresh_inferred", status=TraitStatus.INFERRED, last_detected=now)
    )

    pruned = await mem.prune_stale_traits(user_id, stale_days=14)
    assert pruned == 1  # only the stale inferred one

    current = {t.key for t in await mem.get_current_traits(user_id)}
    assert "stale_inferred" not in current  # closed
    assert "old_confirmed" in current  # confirmed survives despite age
    assert "fresh_inferred" in current  # recently re-detected survives


class FakeConsolidateLLM:
    def __init__(self, groups: list[list[str]]) -> None:
        self._groups = groups

    async def stream_reply(
        self, *, system: str, messages: Sequence[dict]
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        assert system == CONSOLIDATE_SYSTEM
        return json.dumps(self._groups)


async def test_consolidate_merges_crosskey_dupes_only(user_id):
    """Cross-key duplicates (same pattern, different keys, reworded) get merged into the
    highest-confidence one; distinct traits and confirmed traits are never merged."""
    mem = GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)
    now = datetime.now(UTC)

    def t(key, *, status=TraitStatus.INFERRED, conf=0.8):
        return InferredTrait(
            user_id=user_id,
            key=key,
            content=key,
            confidence=conf,
            valid_from=now,
            status_updated_at=now,
            status=status,
            provenance=ProvenanceRecord(episode_ids=["e1"]),
        )

    await mem._graph.insert_trait(t("dupe_a", conf=0.7))
    await mem._graph.insert_trait(t("dupe_b", conf=0.9))  # higher confidence — kept
    await mem._graph.insert_trait(t("distinct", conf=0.8))
    await mem._graph.insert_trait(t("confirmed_one", status=TraitStatus.CONFIRMED, conf=0.95))

    # Model groups the two dupes; also (wrongly) groups a confirmed one — must be ignored.
    llm = FakeConsolidateLLM([["dupe_a", "dupe_b"], ["confirmed_one", "distinct"]])
    merged = await sleep_pass.consolidate(mem, llm, user_id, Settings())

    assert merged == 1  # only one node closed (dupe_a)
    current = {x.key for x in await mem.get_current_traits(user_id)}
    assert "dupe_b" in current  # higher-confidence kept
    assert "dupe_a" not in current  # merged away
    assert "distinct" in current  # untouched
    assert "confirmed_one" in current  # confirmed never merged


def test_parse_detection_salvages_truncated_array():
    """A response cut off mid-object (max_tokens) must NOT throw away the complete
    traits before the cutoff — that silently zeroed high-signal users (Bug 1)."""
    truncated = (
        "[\n"
        '  {"key": "a", "content": "one", "confidence": 0.9, '
        '"provenance_episode_ids": ["e1"], "provenance_signal_ids": []},\n'
        '  {"key": "b", "content": "two", "confidence": 0.8, '
        '"provenance_episode_ids": [], "provenance_signal_ids": ["s1"]},\n'
        '  {"key": "c", "content": "thr'  # cut off mid-string, no closing ] or }
    )
    traits = parse_detection(
        truncated, user_id="u", known_episode_ids={"e1"}, known_signal_ids={"s1"}
    )
    assert [t.key for t in traits] == ["a", "b"]  # the two complete objects survive


def test_parse_detection_accepts_prefixed_provenance_ids():
    """The model sometimes echoes the tag prefix (ep:/sig:); those must still match
    the bare known ids and be stored as bare ids (Bug 1, secondary)."""
    raw = (
        '[{"key": "k", "content": "c", "confidence": 0.9, '
        '"provenance_episode_ids": ["ep:e1"], "provenance_signal_ids": ["sig:s1"]}]'
    )
    traits = parse_detection(raw, user_id="u", known_episode_ids={"e1"}, known_signal_ids={"s1"})
    assert len(traits) == 1
    assert traits[0].provenance.episode_ids == ["e1"]  # prefix stripped
    assert traits[0].provenance.signal_ids == ["s1"]


def test_detection_request_feeds_back_tracked_keys():
    """The model is shown existing keys + status so it can reuse them (idempotency)."""
    now = datetime.now(UTC)
    tracked = InferredTrait(
        user_id="u",
        key="energized_by_sister",
        content="lights up about his sister",
        confidence=0.95,
        valid_from=now,
        status_updated_at=now,
        status=TraitStatus.CONFIRMED,
        provenance=ProvenanceRecord(episode_ids=["e1"]),
    )
    req = build_detection_request([], [], [tracked])
    content = req[0]["content"]
    assert "Patterns already tracked" in content
    assert "[energized_by_sister]" in content
    assert "(confirmed)" in content
