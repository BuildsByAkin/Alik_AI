"""write_commitments soft-dedup of unresolved duplicates (Phase 5.1 / 5.4).

A chatty user re-stating the same intent was creating a new node every day (pile-up).
Phase 5.4: merge on KEY ALONE. Extraction now feeds open commitments back so the model
reuses a key only for the SAME intent, so an OPEN commitment with the same key -> bump
mention_count, refresh expected_by, no new node — EVEN when reworded beyond the old
difflib 0.6 gate (which only blocked the very restatements we want to merge). Resolved
nodes are never merged into (history preserved).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

from alik import sleep_pass
from alik.config import Settings
from alik.memory.graph import GraphMemory
from alik.models import CommitmentNode, CommitmentStatus
from alik.prompt import COMMITMENT_CONSOLIDATE_SYSTEM
from tests.conftest import InMemoryGraphStore, InMemoryMemory


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


def _commit(
    user_id, content, *, key="other:race", expected_by=None, status=CommitmentStatus.PENDING
):
    return CommitmentNode(
        user_id=user_id,
        key=key,
        content=content,
        valid_from=datetime.now(UTC),
        status=status,
        expected_by=expected_by,
    )


async def test_restating_same_commitment_dedups(user_id):
    mem = _mem()
    soon = datetime.now(UTC) + timedelta(days=2)
    await mem.write_commitments([_commit(user_id, "sign up for the half marathon")])
    # Re-stated next day, slightly reworded + now with a time.
    await mem.write_commitments(
        [_commit(user_id, "sign up for the half-marathon race", expected_by=soon)]
    )

    open_commits = await mem.get_open_commitments(user_id)
    assert len(open_commits) == 1  # no pile-up
    c = open_commits[0]
    assert c.mention_count == 2  # bumped
    assert c.expected_by == soon  # refreshed from the more specific restatement


async def test_same_key_reworded_restatement_merges(user_id):
    """Phase 5.4: a same-key restatement reworded BELOW the old 0.6 difflib gate now merges
    (key reuse already means same intent). This is the start_therapy pile-up case (the live
    run scored two reworded statements at 0.07 and inserted a duplicate under the reused key)."""
    mem = _mem()
    await mem.write_commitments(
        [
            _commit(
                user_id,
                "Look into finding a therapist who specializes in anxiety and perfectionism",
                key="other:start_therapy",
            )
        ]
    )
    # Same intent, same reused key, worded completely differently (difflib ~0.07).
    await mem.write_commitments(
        [
            _commit(
                user_id,
                "Gather names of therapists this week without pressure to call immediately",
                key="other:start_therapy",
            )
        ]
    )

    open_commits = await mem.get_open_commitments(user_id)
    assert len(open_commits) == 1  # merged on key alone — no pile-up
    assert open_commits[0].mention_count == 2


async def test_resolved_commitment_not_merged_into(user_id):
    mem = _mem()
    c = _commit(user_id, "run a 10k")
    await mem.write_commitments([c])
    # Resolve it, then re-state the same intent -> a fresh open node, history preserved.
    await mem.resolve_commitment(c.id, kept=True)
    await mem.write_commitments([_commit(user_id, "run a 10k")])

    open_commits = await mem.get_open_commitments(user_id)
    assert len(open_commits) == 1  # the new open one
    assert open_commits[0].id != c.id
    assert mem._graph._commitments[c.id].status is CommitmentStatus.RESOLVED_KEPT  # untouched


class FakeCommitConsolidateLLM:
    """Groups the open commitments whose rendered line mentions 'carlos' (order-agnostic)."""

    async def stream_reply(
        self, *, system: str, messages: Sequence[dict]
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        assert system == COMMITMENT_CONSOLIDATE_SYSTEM
        carlos = []
        for line in messages[0]["content"].splitlines():
            line = line.strip()
            if line.startswith("[") and "carlos" in line.lower():
                carlos.append(int(line[1 : line.index("]")]))
        return json.dumps([carlos] if len(carlos) >= 2 else [])


async def test_consolidate_commitments_merges_crosskey_dupes(user_id):
    """Semantic cross-key dedup: the same intent restated under different keys collapses to
    one open commitment; distinct + resolved commitments are untouched."""
    mem = _mem()
    # Three 'have a hard conversation with Carlos' restatements under DIFFERENT keys...
    await mem.write_commitments(
        [
            _commit(user_id, "have an honest conversation with Carlos", key="other:carlos_1"),
            _commit(user_id, "tell Carlos the truth about feeling stuck", key="other:carlos_2"),
            _commit(user_id, "finally talk to Carlos about being unhappy", key="other:carlos_3"),
            _commit(user_id, "attend pottery class on Thursday", key="other:pottery"),
        ]
    )
    # ...plus a resolved one that must never be merged.
    done = _commit(user_id, "run the 10k", key="other:race")
    await mem.write_commitments([done])
    await mem.resolve_commitment(done.id, kept=True)

    merged = await sleep_pass.consolidate_commitments(
        mem, FakeCommitConsolidateLLM(), user_id, Settings()
    )
    assert merged == 2  # three Carlos dupes -> one kept, two closed

    open_contents = [c.content for c in await mem.get_open_commitments(user_id)]
    assert len(open_contents) == 2  # one Carlos + pottery
    assert sum("carlos" in c.lower() for c in open_contents) == 1  # collapsed to one
    assert any("pottery" in c.lower() for c in open_contents)  # distinct untouched
    assert mem._graph._commitments[done.id].status is CommitmentStatus.RESOLVED_KEPT  # untouched
