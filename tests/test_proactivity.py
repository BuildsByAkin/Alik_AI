"""The proactivity engine's decision logic, over the in-memory double.

Proves the priority order and the one-checkin-per-user rule: due commitment queues a
DUE_COMMITMENT; an upcoming one queues UPCOMING_COMMITMENT; a lapsed user queues a
GENERAL_CHECKIN; nothing queues when a check-in already exists or no condition is met.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

from alik import proactivity
from alik.config import Settings
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, CommitmentNode, CommitmentStatus, MemoryRecord, MemoryTier
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class FakeLLM:
    async def stream_reply(
        self, *, system: str, messages: Sequence[dict]
    ) -> AsyncIterator[str]:  # pragma: no cover
        yield ""

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        return "How are you feeling about that lately?"


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


def _commit(user_id, content, *, status, expected_by=None):
    return CommitmentNode(
        user_id=user_id,
        key=f"other:{content}",
        content=content,
        valid_from=datetime.now(UTC),
        status=status,
        expected_by=expected_by,
    )


async def _episode(mem, user_id, *, days_ago):
    await mem.write(
        MemoryRecord(
            user_id=user_id,
            session_id="s",
            tier=MemoryTier.EPISODIC,
            content="chatted",
            created_at=datetime.now(UTC) - timedelta(days=days_ago),
        )
    )


async def test_due_commitment_queues_followup(user_id):
    mem = _mem()
    await mem.write_commitments(
        [_commit(user_id, "sign up for the race", status=CommitmentStatus.DUE)]
    )

    queued = await proactivity.decide_for_user(mem, FakeLLM(), user_id, Settings())
    assert queued is CheckinType.DUE_COMMITMENT

    checkin = await mem.get_pending_checkin(user_id)
    assert checkin is not None
    assert checkin.checkin_type is CheckinType.DUE_COMMITMENT
    assert checkin.commitment_id is not None
    assert checkin.message_hint
    # The commitment was marked reminded so we don't re-ask it today.
    assert (await mem.get_open_commitments(user_id))[0].reminded_count == 1


async def test_upcoming_commitment_queues_headsup(user_id):
    mem = _mem()
    soon = datetime.now(UTC) + timedelta(hours=6)
    await mem.write_commitments(
        [_commit(user_id, "call the clinic", status=CommitmentStatus.PENDING, expected_by=soon)]
    )

    queued = await proactivity.decide_for_user(mem, FakeLLM(), user_id, Settings())
    assert queued is CheckinType.UPCOMING_COMMITMENT
    assert (await mem.get_pending_checkin(user_id)).checkin_type is CheckinType.UPCOMING_COMMITMENT


async def test_lapsed_user_queues_general_checkin(user_id):
    mem = _mem()
    await _episode(mem, user_id, days_ago=4)  # > proactivity_lapsed_days (3), no commitments

    queued = await proactivity.decide_for_user(mem, FakeLLM(), user_id, Settings())
    assert queued is CheckinType.GENERAL_CHECKIN
    checkin = await mem.get_pending_checkin(user_id)
    assert checkin.checkin_type is CheckinType.GENERAL_CHECKIN
    assert checkin.commitment_id is None


async def test_no_duplicate_when_one_already_pending(user_id):
    mem = _mem()
    await mem.write_commitments([_commit(user_id, "x", status=CommitmentStatus.DUE)])
    # First pass queues one.
    assert await proactivity.decide_for_user(mem, FakeLLM(), user_id, Settings()) is not None
    # Second pass: an undelivered check-in already exists -> skip.
    assert await proactivity.decide_for_user(mem, FakeLLM(), user_id, Settings()) is None
    # Still exactly one queued.
    pending = [c for c in mem._base._checkins if c.user_id == user_id and c.delivered_at is None]
    assert len(pending) == 1


async def test_nothing_queued_when_no_condition(user_id):
    mem = _mem()
    await _episode(mem, user_id, days_ago=0)  # recent session, no commitments
    assert await proactivity.decide_for_user(mem, FakeLLM(), user_id, Settings()) is None
    assert await mem.get_pending_checkin(user_id) is None
