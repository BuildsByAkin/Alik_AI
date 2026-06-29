"""InMemoryStore semantics: shared-interest split, by-interest filtering, delete wipes all."""

from __future__ import annotations

import pytest

from connections_service.models import DimensionSnapshot, InterestEdge, UserPoolEntry
from connections_service.store import InMemoryStore


def _entry(uid: str, *, state: str = "MN", ready: bool = True) -> UserPoolEntry:
    return UserPoolEntry(user_id=uid, state=state, pool_ready=ready)


def _edge(node_id: str, w: float = 1.0) -> InterestEdge:
    return InterestEdge(node_id, w, "primary_hobby")


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()  # self-seeds the taxonomy


async def test_shared_interests_specific_and_broad(store: InMemoryStore):
    await store.upsert_user_interests("a", [_edge("outdoor_active:running"), _edge("gaming:dnd")])
    await store.upsert_user_interests(
        "b", [_edge("outdoor_active:running"), _edge("creative:writing")]
    )

    shared = await store.get_shared_interests("a", "b")
    assert [n.id for n in shared.specific] == ["outdoor_active:running"]
    assert shared.broad == ["outdoor_active"]  # the only category both have


async def test_broad_overlap_without_specific_is_the_coldstart_fallback(store: InMemoryStore):
    await store.upsert_user_interests("a", [_edge("outdoor_active:hiking")])
    await store.upsert_user_interests("b", [_edge("outdoor_active:running")])

    shared = await store.get_shared_interests("a", "b")
    assert shared.specific == []  # no exact node overlap
    assert shared.broad == ["outdoor_active"]  # but the broad category still connects them


async def test_get_users_by_interest_filters_pool_ready_and_state(store: InMemoryStore):
    await store.upsert_user_pool(_entry("a"))
    await store.upsert_user_pool(_entry("b", ready=False))
    await store.upsert_user_pool(_entry("c", state="WI"))
    for uid in ("a", "b", "c"):
        await store.upsert_user_interests(uid, [_edge("gaming:dnd")])

    assert await store.get_users_by_interest("gaming:dnd", "MN") == [
        "a"
    ]  # b not ready, c wrong state


async def test_get_pool_users_filters(store: InMemoryStore):
    await store.upsert_user_pool(_entry("a"))
    await store.upsert_user_pool(_entry("b", ready=False))
    assert [e.user_id for e in await store.get_pool_users("MN")] == ["a"]


async def test_delete_user_wipes_everything(store: InMemoryStore):
    await store.upsert_user_pool(_entry("a"))
    await store.upsert_user_interests("a", [_edge("gaming:dnd")])
    await store.upsert_profile_dimensions(
        "a", [DimensionSnapshot("structure_preference", "needs_structure", 0.8, "confirmed")]
    )

    await store.delete_user("a")
    assert await store.get_pool_users("MN") == []
    assert await store.get_user_interests("a") == []
    assert await store.get_profile_dimensions("a") == []
