"""The callback receiver POST /matches/response (token-gated)."""

from __future__ import annotations

from connections_service.models import MatchStateEntry, MatchStatus


async def _seed_shown(store, user_id="a", candidate_id="b"):
    await store.save_match_state(
        MatchStateEntry(user_id=user_id, candidate_id=candidate_id, status=MatchStatus.SHOWN)
    )


async def test_response_requires_service_token(client):
    resp = client.post(
        "/matches/response", json={"user_id": "a", "candidate_id": "b", "accepted": True}
    )
    assert resp.status_code == 401


async def test_response_accepted(client, store, headers):
    await _seed_shown(store)
    resp = client.post(
        "/matches/response",
        headers=headers,
        json={"user_id": "a", "candidate_id": "b", "accepted": True},
    )
    assert resp.status_code == 204
    ms = await store.get_match_state("a", "b")
    assert ms.status is MatchStatus.ACCEPTED and ms.responded_at is not None


async def test_response_skipped(client, store, headers):
    await _seed_shown(store)
    resp = client.post(
        "/matches/response",
        headers=headers,
        json={"user_id": "a", "candidate_id": "b", "accepted": False},
    )
    assert resp.status_code == 204
    ms = await store.get_match_state("a", "b")
    assert ms.status is MatchStatus.SKIPPED
