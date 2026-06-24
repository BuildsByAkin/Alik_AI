"""Graph write path against a real FalkorDB: write a fact node, read it back.

Proves the Cypher binding in GraphStore. Skips unless ALIK_FALKORDB_URL is set.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alik.models import GraphNode, NodeType
from tests.conftest import requires_graph


@requires_graph
async def test_fact_node_round_trips(graph_memory_real, user_id):
    node = GraphNode(
        user_id=user_id,
        type=NodeType.FACT,
        key="home_city",
        content="lives in Lagos",
        valid_from=datetime.now(UTC),
    )
    try:
        await graph_memory_real.write_nodes([node])

        facts = await graph_memory_real.get_current_facts(user_id)
        assert [f.content for f in facts] == ["lives in Lagos"]
        assert facts[0].key == "home_city"
        assert facts[0].valid_until is None
    finally:
        await graph_memory_real.delete(user_id)
