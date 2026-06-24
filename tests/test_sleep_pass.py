"""Full sleep pass over an in-memory double: all four passes, then idempotency.

Seeds a user so each pass has something to do, runs run_for_user twice, and
confirms a second run doesn't double-promote, re-resolve, re-decay, or duplicate
the reflection — cron jobs can fire twice.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

from alik import sleep_pass
from alik.config import Settings
from alik.memory.graph import GraphMemory
from alik.models import GraphNode, MemoryRecord, MemoryTier, NodeType
from alik.prompt import REFLECTION_SYSTEM, SALIENCE_SYSTEM
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class FakeSleepLLM:
    """Handles both sleep-pass calls: salience scoring and reflection writing."""

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
                    score = 0.9 if "milestone" in line else 0.1
                    out.append({"index": idx, "score": score})
            return json.dumps(out)
        if system == REFLECTION_SYSTEM:
            return "Marcus is a nurse working toward a half marathon. He has been stressed."
        return ""  # pragma: no cover


async def _seed(mem: GraphMemory, user_id: str) -> None:
    # PROMOTE: one salient + one trivial recent episode.
    for text, age in [("a big milestone today", 1), ("chatted about nothing", 1), ("old", 40)]:
        await mem.write(
            MemoryRecord(
                user_id=user_id,
                session_id="s",
                tier=MemoryTier.EPISODIC,
                content=text,
                created_at=datetime.now(UTC) - timedelta(days=age),
            )
        )
    # REFLECT inputs: a fact, a commitment, an emotional signal.
    await mem.write_nodes(
        [
            GraphNode(
                user_id=user_id,
                type=NodeType.COMMITMENT,
                key="other:race",
                content="will run a half marathon",
                valid_from=datetime.now(UTC),
            ),
            GraphNode(
                user_id=user_id,
                type=NodeType.EMOTIONAL_SIGNAL,
                key="stress_source",
                content="stressed about money",
                valid_from=datetime.now(UTC),
            ),
        ]
    )
    # RESOLVE: two current facts with the same key (drift) — inserted directly to
    # bypass real-time temporal resolution.
    for content, conf in [("nurse", 0.9), ("doctor", 0.6)]:
        await mem._graph.insert_node(
            GraphNode(
                user_id=user_id,
                type=NodeType.FACT,
                key="occupation",
                content=content,
                valid_from=datetime.now(UTC),
                confidence=conf,
            )
        )


async def test_full_sleep_pass_then_idempotent(user_id):
    base = InMemoryMemory()
    mem = GraphMemory(base=base, graph=InMemoryGraphStore(), current_facts_limit=50)
    settings = Settings()
    await _seed(mem, user_id)

    r1 = await sleep_pass.run_for_user(mem, FakeSleepLLM(), user_id, settings)

    # PROMOTE: only the milestone episode.
    assert len(r1.promoted) == 1
    assert {e.content for e in await mem.get_promoted_episodes(user_id)} == {
        "a big milestone today"
    }
    # RESOLVE: the lower-confidence duplicate fact was closed, higher kept.
    assert len(r1.resolved) == 1
    assert r1.resolved[0]["key"] == "occupation"
    assert [f.content for f in await mem.get_current_facts(user_id)] == ["nurse"]
    # DECAY: the 40-day episode soft-decayed.
    assert r1.decayed_episodes == 1
    # REFLECT: a reflection was written.
    assert r1.reflection is not None
    assert await mem.get_reflection(user_id) == r1.reflection

    # --- second run: idempotent --------------------------------------------
    r2 = await sleep_pass.run_for_user(mem, FakeSleepLLM(), user_id, settings)
    assert r2.promoted == []  # already-promoted excluded from candidates
    assert r2.resolved == []  # no duplicates remain
    assert r2.decayed_episodes == 0  # already decayed
    assert len(base._reflections[user_id]) == 1  # no duplicate reflection
