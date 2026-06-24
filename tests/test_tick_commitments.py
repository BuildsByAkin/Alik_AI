"""tick_commitments (the sixth sleep pass) over the in-memory double.

Proves: pending -> due by expected_by; the 14-day fallback fires on null expected_by;
an already-due commitment is not re-marked; the tick NEVER resolves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alik import sleep_pass
from alik.config import Settings
from alik.memory.graph import GraphMemory
from alik.models import CommitmentNode, CommitmentStatus
from tests.conftest import InMemoryGraphStore, InMemoryMemory


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


def _commit(
    user_id, content, *, status=CommitmentStatus.PENDING, expected_by=None, valid_from=None
):
    return CommitmentNode(
        user_id=user_id,
        key=f"other:{content}",
        content=content,
        valid_from=valid_from or datetime.now(UTC),
        status=status,
        expected_by=expected_by,
    )


async def test_pending_goes_due_by_expected_by(user_id):
    mem = _mem()
    now = datetime.now(UTC)
    await mem.write_commitments(
        [
            _commit(user_id, "overdue", expected_by=now - timedelta(hours=1)),
            _commit(user_id, "future", expected_by=now + timedelta(days=2)),
        ]
    )
    ticked = await sleep_pass.tick_commitments(mem, user_id, Settings())
    assert ticked == 1

    by_content = {c.content: c.status for c in await mem.get_open_commitments(user_id)}
    assert by_content["overdue"] is CommitmentStatus.DUE
    assert by_content["future"] is CommitmentStatus.PENDING  # not yet due


async def test_fallback_fires_on_null_expected_by(user_id):
    mem = _mem()
    now = datetime.now(UTC)
    settings = Settings()  # commitment_due_fallback_days = 14
    await mem.write_commitments(
        [
            _commit(user_id, "old", valid_from=now - timedelta(days=20)),  # > 14d, no time
            _commit(user_id, "recent", valid_from=now - timedelta(days=2)),  # < 14d, no time
        ]
    )
    ticked = await sleep_pass.tick_commitments(mem, user_id, settings)
    assert ticked == 1

    by_content = {c.content: c.status for c in await mem.get_open_commitments(user_id)}
    assert by_content["old"] is CommitmentStatus.DUE
    assert by_content["recent"] is CommitmentStatus.PENDING


async def test_already_due_not_remarked_and_never_resolves(user_id):
    mem = _mem()
    now = datetime.now(UTC)
    await mem.write_commitments(
        [
            _commit(user_id, "already", status=CommitmentStatus.DUE),
            _commit(user_id, "overdue", expected_by=now - timedelta(hours=1)),
        ]
    )
    ticked = await sleep_pass.tick_commitments(mem, user_id, Settings())
    assert ticked == 1  # only the pending overdue one, the already-due is untouched

    statuses = {c.status for c in await mem.get_open_commitments(user_id)}
    # Nothing is ever resolved by the tick — only pending/due exist.
    assert statuses <= {CommitmentStatus.PENDING, CommitmentStatus.DUE}
    assert CommitmentStatus.RESOLVED_KEPT not in statuses
    assert CommitmentStatus.RESOLVED_DROPPED not in statuses
