"""Brain HTTP surface for Phase 8: the social-events write-back endpoint, the generalized
check-in endpoint (rendezvous types), and rendezvous in the account-deletion fan-out."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from fastapi.testclient import TestClient

from alik.api import create_app
from alik.companion import Companion
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, SocialEventKind
from alik.prompt import load_persona
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class _FakeLLM:
    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        yield "ok"

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        return "ok"


class _FakeRendezvous:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_user(self, user_id: str) -> None:
        self.deleted.append(user_id)

    async def aclose(self) -> None:
        pass


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


def _client(mem: GraphMemory, rendezvous=None) -> TestClient:
    companion = Companion(memory=mem, llm=_FakeLLM(), persona=load_persona(), episode_limit=10)
    return TestClient(create_app(companion=companion, memory=mem, rendezvous_client=rendezvous))


async def test_social_event_endpoint_records_to_memory():
    mem = _mem()
    resp = _client(mem).post(
        "/users/u1/social-events",
        json={
            "kind": "meet_set",
            "summary": "arranged to meet someone who loves pottery — Uptown / Saturday",
            "source": "rendezvous",
            "counterpart_ref": "u2",
        },
    )
    assert resp.status_code == 200
    events = await mem.get_recent_social_events("u1")
    assert len(events) == 1
    assert events[0].kind is SocialEventKind.MEET_SET
    assert events[0].counterpart_ref == "u2"


def test_social_event_endpoint_rejects_unknown_kind():
    resp = _client(_mem()).post(
        "/users/u1/social-events",
        json={"kind": "not_a_kind", "summary": "x", "source": "rendezvous"},
    )
    assert resp.status_code == 400


async def test_checkin_endpoint_accepts_rendezvous_types():
    mem = _mem()
    resp = _client(mem).post(
        "/users/u1/checkins",
        json={"type": "rendezvous_pref", "reason": "meet the pottery person", "meet_id": "m1"},
    )
    assert resp.status_code == 200
    pending = await mem.get_pending_checkin("u1")
    assert pending.checkin_type is CheckinType.RENDEZVOUS_PREF
    assert pending.payload["meet_id"] == "m1"


def test_checkin_endpoint_rejects_unknown_type():
    resp = _client(_mem()).post(
        "/users/u1/checkins", json={"type": "bogus", "reason": "x"}
    )
    assert resp.status_code == 400


def test_delete_fans_out_to_rendezvous():
    rendezvous = _FakeRendezvous()
    resp = _client(_mem(), rendezvous=rendezvous).delete("/users/u1")
    assert resp.status_code == 200
    assert rendezvous.deleted == ["u1"]  # erasure reached the rendezvous service
