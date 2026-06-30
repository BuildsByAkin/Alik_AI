"""Fixtures: a TestClient over the app with an in-memory store + a fake brain (no infra).

The service token is patched onto the settings singleton (deps reads it there) and every
gated request carries it via the ``headers`` fixture.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from connections_service import deps
from connections_service.main import create_app
from connections_service.store import InMemoryStore

TOKEN = "test-service-token"


class FakeBrain:
    """Stands in for the brain Profile API. ``fetch_profile`` returns the per-user profile
    that was ``set`` (or None if set to None / never set — i.e. a fetch failure)."""

    def __init__(self) -> None:
        self.profiles: dict[str, dict | None] = {}
        self.queued: list[tuple[str, object]] = []  # (user_id, MatchCheckin) for queue_checkin
        self.checkin_id: str | None = "ck1"  # set to None to simulate a queue failure

    def set(self, user_id: str, profile: dict | None) -> None:
        self.profiles[user_id] = profile

    async def fetch_profile(self, user_id: str) -> dict | None:
        return self.profiles.get(user_id)

    async def get_profile(self, user_id: str) -> dict:
        return await self.fetch_profile(user_id) or {
            "identity": None,
            "facts": [],
            "confirmed_traits": [],
            "dimensions": [],
        }

    async def queue_checkin(self, user_id: str, checkin) -> str | None:
        self.queued.append((user_id, checkin))
        return self.checkin_id

    async def aclose(self) -> None:
        pass


class FakeAuth:
    """Stands in for the auth roster endpoint: state -> [user_id]."""

    def __init__(self, roster: dict[str, list[str]] | None = None) -> None:
        self.roster = roster or {}

    async def list_user_ids(self, state: str, **_kw) -> list[str]:
        return list(self.roster.get(state, []))

    async def aclose(self) -> None:
        pass


def make_profile(
    *,
    state: str | None = "MN",
    age: int = 31,
    city: str = "Minneapolis",
    facts: dict[str, str] | None = None,
    dimensions: list[dict] | None = None,
    traits: list[dict] | None = None,
) -> dict:
    """Build a Profile-API-shaped dict. ``state=None`` => no identity (auth unavailable)."""
    identity = None
    if state is not None:
        identity = {"id": "x", "name": "A", "age": age, "city": city, "state": state}
    return {
        "user_id": "x",
        "identity": identity,
        "facts": [{"key": k, "content": v} for k, v in (facts or {}).items()],
        "confirmed_traits": traits or [],
        "dimensions": dimensions or [],
    }


def dim(dimension: str, value: str, confidence: float, status: str = "confirmed") -> dict:
    return {
        "dimension": dimension,
        "value": value,
        "content": "",
        "confidence": confidence,
        "status": status,
    }


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
