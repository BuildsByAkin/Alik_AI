"""Fixtures: a TestClient over the app with an in-memory store + a fake brain (no infra)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from rendezvous_service import deps
from rendezvous_service.main import create_app
from rendezvous_service.store import InMemoryStore

TOKEN = "test-service-token"


class FakeBrain:
    """Stands in for the brain. Records queued check-ins + social events; ``checkin_id`` can be
    set to None to simulate a queue failure (so the advance pass shouldn't set the asked-flag)."""

    def __init__(self) -> None:
        self.checkins: list[tuple[str, str, str, str]] = []  # (user, type, reason, meet_id)
        self.social_events: list[
            tuple[str, str, str, str | None]
        ] = []  # (user, kind, summary, ref)
        self.checkin_id: str | None = "ck1"

    async def queue_checkin(self, user_id, checkin_type, reason, *, meet_id) -> str | None:
        self.checkins.append((user_id, checkin_type, reason, meet_id))
        return self.checkin_id

    async def record_social_event(self, user_id, kind, summary, *, counterpart_ref=None) -> bool:
        self.social_events.append((user_id, kind, summary, counterpart_ref))
        return True

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _token(monkeypatch) -> None:
    monkeypatch.setattr(deps.settings, "service_token", SecretStr(TOKEN))


@pytest.fixture
def headers() -> dict:
    return {"X-Service-Token": TOKEN}


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def brain() -> FakeBrain:
    return FakeBrain()


@pytest.fixture
def client(store: InMemoryStore, brain: FakeBrain) -> TestClient:
    return TestClient(create_app(store=store, brain_client=brain))
