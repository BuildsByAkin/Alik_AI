"""Full run_for_user including the DETECT pass, over the in-memory double.

Asserts a trait lands with provenance, that a raising DETECT is isolated (the
earlier passes still complete), and that a second run is idempotent on traits.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

from alik import sleep_pass
from alik.config import Settings
from alik.memory.graph import GraphMemory
from alik.models import GraphNode, MemoryRecord, MemoryTier, NodeType
from alik.prompt import DETECTION_SYSTEM, REFLECTION_SYSTEM, SALIENCE_SYSTEM
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class FakeFullLLM:
    """Handles all three LLM-backed passes: salience, reflection, detection."""

    async def stream_reply(
        self, *, system: str, messages: Sequence[dict]
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        if system == SALIENCE_SYSTEM:
            out = []
            for line in messages[0]["content"].splitlines():
                if line.startswith("["):
                    idx = int(line[1 : line.index("]")])
                    out.append({"index": idx, "score": 0.9 if "milestone" in line else 0.1})
            return json.dumps(out)
        if system == REFLECTION_SYSTEM:
            return "A reflection about the person."
        if system == DETECTION_SYSTEM:
            text = messages[0]["content"]
            ep_ids = [tok[4:-1] for tok in text.split() if tok.startswith("[ep:")]
            sig_ids = [tok[5:-1] for tok in text.split() if tok.startswith("[sig:")]
            return json.dumps(
                [
                    {
                        "key": "anxiety_before_decisions",
                        "content": "gets anxious before big decisions",
                        "confidence": 0.75,
                        "provenance_episode_ids": ep_ids[:1],
                        "provenance_signal_ids": sig_ids[:1],
                    }
                ]
            )
        return ""  # pragma: no cover


async def _seed(mem: GraphMemory, user_id: str) -> None:
    await mem.write(
        MemoryRecord(
            user_id=user_id,
            session_id="s",
            tier=MemoryTier.EPISODIC,
            content="a big milestone today",
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await mem.write_nodes(
        [
            GraphNode(
                user_id=user_id,
                type=NodeType.EMOTIONAL_SIGNAL,
                key="anxiety_level",
                content="nervous ahead of a decision",
                valid_from=datetime.now(UTC),
            )
        ]
    )


async def test_run_for_user_detects_trait_with_provenance(user_id):
    mem = GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)
    await _seed(mem, user_id)

    r1 = await sleep_pass.run_for_user(mem, FakeFullLLM(), user_id, Settings())
    assert r1.traits_detected == ["anxiety_before_decisions"]

    traits = await mem.get_current_traits(user_id)
    assert len(traits) == 1
    assert traits[0].provenance.episode_ids  # provenance landed
    assert traits[0].provenance.signal_ids

    # Idempotent: same content on a second run is a no-op (still one current trait).
    r2 = await sleep_pass.run_for_user(mem, FakeFullLLM(), user_id, Settings())
    assert r2.traits_detected == ["anxiety_before_decisions"]
    assert len(await mem.get_current_traits(user_id)) == 1


async def test_detect_failure_is_isolated(user_id, monkeypatch):
    mem = GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)
    await _seed(mem, user_id)

    async def boom(*args, **kwargs):
        raise RuntimeError("detect exploded")

    monkeypatch.setattr(sleep_pass, "detect", boom)

    # A raising DETECT must not sink the rest of the run.
    report = await sleep_pass.run_for_user(mem, FakeFullLLM(), user_id, Settings())
    assert report.reflection is not None  # REFLECT still ran
    assert report.promoted  # PROMOTE still ran
    assert report.traits_detected == []  # DETECT produced nothing
