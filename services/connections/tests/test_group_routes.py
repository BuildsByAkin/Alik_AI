"""POST /matches/group-response (token-gated): decline declines the group; accept is a no-op."""

from __future__ import annotations

from connections_service.models import GroupCandidate, GroupStatus


async def _seed_group(store, group_id="g1", status=GroupStatus.SURFACED):
    await store.save_group_candidate(
        GroupCandidate(
            group_id=group_id,
            interest_node_id="outdoor_active:running",
            member_ids=["a", "b", "c"],
            mean_score=0.7,
            status=status,
        )
    )


async def test_group_response_requires_token(client):
    resp = client.post(
        "/matches/group-response", json={"user_id": "a", "group_id": "g1", "accepted": False}
    )
    assert resp.status_code == 401


async def test_one_decline_declines_the_group(client, store, headers):
    await _seed_group(store)
    resp = client.post(
        "/matches/group-response",
        headers=headers,
        json={"user_id": "a", "group_id": "g1", "accepted": False},
    )
    assert resp.status_code == 204
    assert (await store.get_group_candidate("g1")).status is GroupStatus.DECLINED


async def test_accept_does_not_change_status(client, store, headers):
    await _seed_group(store, status=GroupStatus.SURFACED)
    resp = client.post(
        "/matches/group-response",
        headers=headers,
        json={"user_id": "a", "group_id": "g1", "accepted": True},
    )
    assert resp.status_code == 204
    assert (await store.get_group_candidate("g1")).status is GroupStatus.SURFACED
