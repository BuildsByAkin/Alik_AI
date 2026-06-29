"""The Profile API (assembled living profile) and cross-service account deletion.

Both run against the in-memory doubles with a fake auth client (no network): the API
assembles identity + facts + confirmed traits + dimensions, and DELETE fans erasure out
to both the brain's memory and the auth service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from alik.api import create_app
from alik.companion import Companion
from alik.memory.graph import GraphMemory
from alik.models import (
    DimensionStatus,
    GraphNode,
    InferredTrait,
    NodeType,
    ProfileDimension,
    ProvenanceRecord,
    TraitStatus,
)
from alik.prompt import load_persona
from tests.conftest import InMemoryGraphStore, InMemoryMemory


class _FakeLLM:
    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        yield "ok "

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        return "ok"


class _FakeAuthClient:
    def __init__(self, profile: dict | None = None) -> None:
        self.profile = profile
        self.deleted: list[str] = []

    async def get_profile(self, user_id: str) -> dict | None:
        return self.profile

    async def delete_user(self, user_id: str) -> None:
        self.deleted.append(user_id)

    async def aclose(self) -> None:
        pass


def _mem() -> GraphMemory:
    return GraphMemory(base=InMemoryMemory(), graph=InMemoryGraphStore(), current_facts_limit=50)


def _app(mem: GraphMemory, auth: _FakeAuthClient) -> TestClient:
    companion = Companion(memory=mem, llm=_FakeLLM(), persona=load_persona(), episode_limit=10)
    return TestClient(create_app(companion=companion, memory=mem, auth_client=auth))


async def _seed(mem: GraphMemory, user_id: str) -> None:
    now = datetime.now(UTC)
    await mem.write_nodes(
        [
            GraphNode(
                user_id=user_id,
                type=NodeType.FACT,
                key="primary_hobby",
                content="plays chess",
                valid_from=now,
            )
        ]
    )
    await mem.write_traits(
        [
            InferredTrait(
                user_id=user_id,
                key="energized_by_chess",
                content="lights up over chess",
                confidence=0.9,
                valid_from=now,
                status_updated_at=now,
                status=TraitStatus.CONFIRMED,
                provenance=ProvenanceRecord(episode_ids=["e1"]),
            ),
            InferredTrait(
                user_id=user_id,
                key="quiet_mornings",
                content="prefers quiet mornings",
                confidence=0.7,
                valid_from=now,
                status_updated_at=now,
                status=TraitStatus.INFERRED,
                provenance=ProvenanceRecord(episode_ids=["e2"]),
            ),
        ]
    )
    await mem.put_profile_dimension(
        ProfileDimension(
            user_id=user_id,
            dimension="interest_intensity",
            value="intense_specific",
            content="intensely into chess specifically",
            confidence=0.8,
            valid_from=now,
            updated_at=now,
            status=DimensionStatus.CONFIRMED,
        )
    )


async def test_profile_api_assembles_everything(user_id):
    mem = _mem()
    await _seed(mem, user_id)
    auth = _FakeAuthClient(profile={"name": "Avery", "age": 31, "city": "Lagos"})
    client = _app(mem, auth)

    body = client.get(f"/users/{user_id}/profile").json()

    assert body["identity"] == {"name": "Avery", "age": 31, "city": "Lagos"}
    assert body["facts"] == [{"key": "primary_hobby", "content": "plays chess"}]
    # Only the CONFIRMED trait is exposed; the INFERRED one is not.
    assert [t["key"] for t in body["confirmed_traits"]] == ["energized_by_chess"]
    assert body["dimensions"][0]["dimension"] == "interest_intensity"
    assert body["dimensions"][0]["status"] == "confirmed"


async def test_profile_api_degrades_without_identity(user_id):
    mem = _mem()
    await _seed(mem, user_id)
    auth = _FakeAuthClient(profile=None)  # auth down / no row
    client = _app(mem, auth)

    body = client.get(f"/users/{user_id}/profile").json()
    assert body["identity"] is None
    assert body["dimensions"]  # the rest still assembles


async def test_cross_service_delete_erases_brain_and_auth(user_id):
    mem = _mem()
    await _seed(mem, user_id)
    auth = _FakeAuthClient()
    client = _app(mem, auth)

    resp = client.delete(f"/users/{user_id}")

    assert resp.status_code == 200
    assert auth.deleted == [user_id]  # auth erasure invoked
    assert await mem.get_profile_dimensions(user_id) == []  # brain erased
    assert await mem.get_current_facts(user_id) == []
