"""Temporal resolution: a contradicting Fact supersedes the old one by key.

Runs infra-free against the in-memory graph double, so it proves the policy in
GraphMemory (not FalkorDB).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alik.models import GraphNode, NodeType


def _fact(user_id: str, content: str, *, when: datetime) -> GraphNode:
    return GraphNode(
        user_id=user_id,
        type=NodeType.FACT,
        key="primary_exercise",  # same entity -> the two facts contradict
        content=content,
        valid_from=when,
    )


async def test_new_fact_supersedes_old_for_same_entity(graph_memory_fake, user_id):
    t0 = datetime.now(UTC)
    await graph_memory_fake.write_nodes([_fact(user_id, "climbs", when=t0)])
    await graph_memory_fake.write_nodes(
        [_fact(user_id, "trail runs", when=t0 + timedelta(hours=1))]
    )

    current = await graph_memory_fake.get_current_facts(user_id)

    # Only the latest truth is current; "climbs" was closed, not returned.
    assert [f.content for f in current] == ["trail runs"]
    assert all(f.valid_until is None for f in current)
