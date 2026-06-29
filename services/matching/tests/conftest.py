"""Fixtures: a TestClient over the app with an in-memory store + a fake brain (no infra).

The service token is patched onto the settings singleton (deps reads it there) and every
request carries it via the ``headers`` fixture.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from matching_service import deps
from matching_service.catalog import load_catalog
from matching_service.main import create_app
from matching_service.store import InMemoryStore

TOKEN = "test-service-token"
CATALOG = load_catalog("data/jobs.json")


class FakeBrain:
    """Stands in for the brain Profile API."""

    def __init__(self, facts=None, confirmed_traits=None) -> None:
        self.profile = {"facts": facts or [], "confirmed_traits": confirmed_traits or []}

    async def get_profile(self, user_id: str) -> dict:
        return self.profile

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
    return TestClient(create_app(store=store, brain_client=brain, catalog=CATALOG))
