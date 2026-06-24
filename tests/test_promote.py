"""PROMOTE pass: salient episodes (score > threshold) get promoted; idempotent.

Infra-free: fake salience LLM + in-memory doubles.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from alik import sleep_pass
from alik.config import Settings
from alik.memory.graph import GraphMemory
from alik.models import MemoryRecord, MemoryTier
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class FakeSalienceLLM:
    """Scores each listed episode by content substring (robust to candidate order)."""

    def __init__(self, scores_by_content: dict[str, float]) -> None:
        self._scores = scores_by_content
        self.calls = 0

    async def stream_reply(
        self, *, system: str, messages: Sequence[dict]
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        self.calls += 1
        listing = messages[0]["content"]
        out = []
        for line in listing.splitlines():
            if not line.startswith("["):
                continue
            idx = int(line[1 : line.index("]")])
            score = next((s for sub, s in self._scores.items() if sub in line), 0.0)
            out.append({"index": idx, "score": score})
        return json.dumps(out)


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


async def _seed_episode(mem: GraphMemory, user_id: str, summary: str) -> None:
    await mem.write(
        MemoryRecord(
            user_id=user_id,
            session_id="s",
            tier=MemoryTier.EPISODIC,
            content=summary,
            created_at=datetime.now(UTC),
        )
    )


async def test_promote_only_above_threshold_and_idempotent(user_id):
    mem = _mem()
    for text in ["bought a house", "talked about weather", "got engaged"]:
        await _seed_episode(mem, user_id, text)

    llm = FakeSalienceLLM(
        {"house": 0.9, "weather": 0.2, "engaged": 0.85}
    )  # promote house + engaged, skip weather
    settings = Settings()

    promoted = await sleep_pass.promote(mem, llm, user_id, settings)
    assert len(promoted) == 2

    contents = {e.content for e in await mem.get_promoted_episodes(user_id)}
    assert contents == {"bought a house", "got engaged"}

    # Idempotent re-run: already-promoted episodes drop out of the candidate set,
    # so nothing new is promoted and the LLM isn't re-invoked on them.
    promoted_again = await sleep_pass.promote(mem, llm, user_id, settings)
    assert promoted_again == []
    assert len(await mem.get_promoted_episodes(user_id)) == 2
