"""Test fixtures: a FastAPI TestClient backed by an in-memory fake Supabase (no network).

The service reaches Supabase only through ``supabase_client.get_anon_client`` /
``get_service_client``. The services import those names directly, so we patch them where
they are *used* (in ``auth_svc`` / ``profile_svc``) and return a fake client whose calls
the individual tests configure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from auth_service import deps
from auth_service.main import app
from auth_service.services import auth_svc, profile_svc

# --- Fake Supabase building blocks ------------------------------------------------------


class FakeQuery:
    """A chainable table query whose ``execute()`` returns a preset response."""

    def __init__(self, result: Any) -> None:
        self._result = result

    def insert(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def select(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def update(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def delete(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def eq(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    def single(self, *_a: Any, **_k: Any) -> FakeQuery:
        return self

    async def execute(self) -> Any:
        return SimpleNamespace(data=self._result)


class FakeStorageBucket:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, bytes, dict]] = []
        self.removed: list[list[str]] = []

    async def upload(self, path: str, data: bytes, opts: dict) -> Any:
        self.uploaded.append((path, data, opts))
        return SimpleNamespace(path=path)

    def get_public_url(self, path: str) -> str:
        return f"https://fake.supabase.co/storage/v1/object/public/profile-photos/{path}"

    async def remove(self, paths: list[str]) -> Any:
        self.removed.append(paths)
        return SimpleNamespace(data=[])


class FakeClient:
    """A configurable stand-in for an async Supabase client."""

    def __init__(self) -> None:
        self.auth = SimpleNamespace(admin=SimpleNamespace())
        self._tables: dict[str, Any] = {}
        self._bucket = FakeStorageBucket()

    # table(...) returns a FakeQuery preloaded with whatever the test registered.
    def set_table_result(self, name: str, result: Any) -> None:
        self._tables[name] = result

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self._tables.get(name))

    def storage_bucket(self) -> FakeStorageBucket:
        return self._bucket

    @property
    def storage(self) -> Any:
        bucket = self._bucket
        return SimpleNamespace(from_=lambda _name: bucket)


# --- Fixtures ---------------------------------------------------------------------------


@pytest.fixture
def fake_anon() -> FakeClient:
    return FakeClient()


@pytest.fixture
def fake_service() -> FakeClient:
    return FakeClient()


@pytest.fixture(autouse=True)
def patch_clients(monkeypatch: pytest.MonkeyPatch, fake_anon: FakeClient, fake_service: FakeClient):
    """Route every get_*_client call (in services + deps) to the fakes."""

    async def _anon() -> FakeClient:
        return fake_anon

    async def _service() -> FakeClient:
        return fake_service

    monkeypatch.setattr(auth_svc, "get_anon_client", _anon)
    monkeypatch.setattr(auth_svc, "get_service_client", _service)
    monkeypatch.setattr(profile_svc, "get_service_client", _service)
    monkeypatch.setattr(deps, "get_anon_client", _anon)
    return None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_as():
    """Override the auth dependency to act as a given user id, then clean up."""

    def _apply(user_id: str) -> None:
        app.dependency_overrides[deps.get_current_user] = lambda: user_id

    yield _apply
    app.dependency_overrides.clear()
