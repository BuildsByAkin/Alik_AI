"""DECAY pass: old non-promoted episodes are soft-decayed; stale facts lose
confidence at most once per window (no nightly compounding). Idempotent.

Infra-free: in-memory doubles.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alik import sleep_pass
from alik.config import Settings
from alik.memory.graph import GraphMemory
from alik.models import GraphNode, MemoryRecord, MemoryTier, NodeType
from tests.conftest import InMemoryGraphStore, InMemoryMemory


async def _episode(mem: GraphMemory, user_id: str, summary: str, *, age_days: int) -> None:
    await mem.write(
        MemoryRecord(
            user_id=user_id,
            session_id="s",
            tier=MemoryTier.EPISODIC,
            content=summary,
            created_at=datetime.now(UTC) - timedelta(days=age_days),
        )
    )


async def test_decay_episodes_and_facts_idempotent(user_id):
    base = InMemoryMemory()
    graph = InMemoryGraphStore()
    # Decay facts unmentioned for 1+ days so the test fact qualifies.
    mem = GraphMemory(base=base, graph=graph, current_facts_limit=50, confidence_decay_days=1)
    settings = Settings()  # decay_after_days = 30

    await _episode(mem, user_id, "old chat", age_days=40)  # decays
    await _episode(mem, user_id, "recent chat", age_days=2)  # kept
    await mem.write_nodes(
        [
            GraphNode(
                user_id=user_id,
                type=NodeType.FACT,
                key="occupation",
                content="nurse",
                valid_from=datetime.now(UTC) - timedelta(days=5),  # stale -> decays
                confidence=1.0,
            )
        ]
    )

    ep_count, fact_count = await sleep_pass.decay(mem, user_id, settings)
    assert ep_count == 1
    assert fact_count == 1

    # The decayed episode is excluded from retrieve(); the recent one remains.
    ctx = await mem.retrieve(user_id)
    assert [e.content for e in ctx.episodes] == ["recent chat"]

    fact = (await mem.get_current_facts(user_id))[0]
    assert fact.confidence == 0.85  # 1.0 * 0.85, decayed once

    # Idempotent re-run: nothing new decays, confidence is NOT compounded.
    ep_count2, fact_count2 = await sleep_pass.decay(mem, user_id, settings)
    assert ep_count2 == 0
    assert fact_count2 == 0
    assert (await mem.get_current_facts(user_id))[0].confidence == 0.85
