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


# --- Phase 8: mutual-accept -> create a rendezvous meet ---------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from connections_service.main import create_app  # noqa: E402
from connections_service.models import InterestEdge  # noqa: E402


class _FakeRendezvous:
    def __init__(self) -> None:
        self.created: list[tuple[str, str, str, str]] = []

    async def create_meet(self, user_a, user_b, desc_a, desc_b) -> str | None:
        self.created.append((user_a, user_b, desc_a, desc_b))
        return "meet-1"

    async def aclose(self) -> None:
        pass


async def test_mutual_accept_creates_meet_with_anonymized_descriptor(store, brain, headers):
    rv = _FakeRendezvous()
    client = TestClient(create_app(store=store, brain_client=brain, rendezvous_client=rv))
    # A already accepted B; both share hiking.
    await store.save_match_state(MatchStateEntry("A", "B", MatchStatus.ACCEPTED))
    await store.upsert_user_interests("A", [InterestEdge("outdoor_active:hiking", 1.0, "h")])
    await store.upsert_user_interests("B", [InterestEdge("outdoor_active:hiking", 1.0, "h")])

    # Now B accepts A -> mutual -> a meet is created.
    r = client.post(
        "/matches/response", headers=headers,
        json={"user_id": "B", "candidate_id": "A", "accepted": True},
    )
    assert r.status_code == 204
    assert len(rv.created) == 1
    ua, ub, da, db = rv.created[0]
    assert {ua, ub} == {"A", "B"}
    assert "Hiking" in da and "someone who also loves" in da  # anonymized, no names


async def test_one_sided_accept_creates_no_meet(store, brain, headers):
    rv = _FakeRendezvous()
    client = TestClient(create_app(store=store, brain_client=brain, rendezvous_client=rv))
    # B accepts A, but A never accepted B.
    r = client.post(
        "/matches/response", headers=headers,
        json={"user_id": "B", "candidate_id": "A", "accepted": True},
    )
    assert r.status_code == 204
    assert rv.created == []
