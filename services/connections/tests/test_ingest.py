"""End-to-end ingestion against InMemoryStore + fake auth/brain — no infra.

Covers: full ingest of a mock Profile API response, the pool_ready floor, and the
keep-last-snapshot-on-brain-failure rule (the crux of the §6 decision)."""

from __future__ import annotations

import pytest

from connections_service.config import Settings
from connections_service.ingest import run_ingest
from connections_service.store import InMemoryStore
from tests.conftest import FakeAuth, FakeBrain, dim, make_profile

SETTINGS = Settings()  # defaults: launch_states=MN, dim floor 0.6, trait floor 0.7


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


async def test_full_ingest_populates_pool_interests_and_dimensions(store):
    brain = FakeBrain()
    brain.set(
        "u1",
        make_profile(
            facts={"primary_hobby": "rock climbing", "music_taste": "jazz"},
            dimensions=[dim("interest_intensity", "intense_specific", 0.9)],
        ),
    )
    auth = FakeAuth({"MN": ["u1"]})

    counts = await run_ingest(store, brain, auth, SETTINGS)

    assert counts == {"ingested": 1, "below_floor": 0, "skipped": 0}
    pool = await store.get_pool_users("MN")
    assert [e.user_id for e in pool] == ["u1"]
    assert pool[0].age == 31 and pool[0].state == "MN" and pool[0].city == "Minneapolis"
    nodes = {e.interest_node_id for e in await store.get_user_interests("u1")}
    assert "outdoor_active:rock_climbing" in nodes and "music_listening:jazz" in nodes
    assert {d.dimension for d in await store.get_profile_dimensions("u1")} == {"interest_intensity"}


async def test_below_floor_is_stored_but_not_pool_ready(store):
    brain = FakeBrain()
    # No interest facts, and the only dimension is below the 0.6 confidence floor.
    brain.set("u2", make_profile(facts={}, dimensions=[dim("topic_focus", "balanced", 0.4)]))
    auth = FakeAuth({"MN": ["u2"]})

    counts = await run_ingest(store, brain, auth, SETTINGS)

    assert counts == {"ingested": 0, "below_floor": 1, "skipped": 0}
    assert await store.get_pool_users("MN") == []  # not surfaced
    assert store._pool["u2"].pool_ready is False  # but stored, flagged


async def test_strong_dimension_alone_meets_floor(store):
    brain = FakeBrain()
    brain.set(
        "u3",
        make_profile(facts={}, dimensions=[dim("structure_preference", "needs_structure", 0.8)]),
    )
    counts = await run_ingest(store, brain, FakeAuth({"MN": ["u3"]}), SETTINGS)
    assert counts["ingested"] == 1


async def test_brain_failure_keeps_last_snapshot(store):
    brain = FakeBrain()
    brain.set(
        "u4",
        make_profile(
            facts={"primary_hobby": "hiking"},
            dimensions=[dim("interest_intensity", "engaged", 0.7)],
        ),
    )
    auth = FakeAuth({"MN": ["u4"]})
    await run_ingest(store, brain, auth, SETTINGS)
    before = store._pool["u4"]

    brain.set("u4", None)  # next cycle: brain fetch fails
    counts = await run_ingest(store, brain, auth, SETTINGS)

    assert counts == {"ingested": 0, "below_floor": 0, "skipped": 1}
    assert store._pool["u4"] is before  # snapshot untouched, last_ingested_at NOT bumped
    assert await store.get_user_interests("u4")  # interests preserved
