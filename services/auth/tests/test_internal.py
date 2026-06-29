"""Internal (service-to-service) endpoints: token gate, identity read, hard erase.

Supabase is fully mocked via the shared conftest fakes; the service token is patched
onto the settings singleton so no real secret is needed.
"""

from __future__ import annotations

from pydantic import SecretStr

from auth_service import deps
from tests.conftest import FakeClient

TOKEN = "test-service-token"

PROFILE_ROW = {
    "id": "uid-7",
    "name": "Avery",
    "age": 31,
    "city": "Lagos",
    "photo_url": None,
    "created_at": "2026-06-26T10:00:00+00:00",
    "updated_at": "2026-06-26T10:00:00+00:00",
}


def _set_token(monkeypatch) -> None:
    monkeypatch.setattr(deps.server_settings, "service_token", SecretStr(TOKEN))


def test_internal_get_profile_requires_token(client, monkeypatch) -> None:
    _set_token(monkeypatch)
    resp = client.get("/internal/profiles/uid-7")  # no X-Service-Token header
    assert resp.status_code == 401


def test_internal_get_profile_rejects_wrong_token(client, monkeypatch) -> None:
    _set_token(monkeypatch)
    resp = client.get("/internal/profiles/uid-7", headers={"X-Service-Token": "wrong"})
    assert resp.status_code == 401


def test_internal_get_profile_ok(client, fake_service: FakeClient, monkeypatch) -> None:
    _set_token(monkeypatch)
    fake_service.set_table_result("profiles", PROFILE_ROW)

    resp = client.get("/internal/profiles/uid-7", headers={"X-Service-Token": TOKEN})

    assert resp.status_code == 200
    assert resp.json()["id"] == "uid-7"
    assert resp.json()["age"] == 31


def test_internal_delete_user_hard_erases(client, fake_service: FakeClient, monkeypatch) -> None:
    _set_token(monkeypatch)
    deleted: dict[str, str] = {}

    async def _delete_user(uid: str) -> None:
        deleted["uid"] = uid

    fake_service.auth.admin.delete_user = _delete_user

    resp = client.delete("/internal/users/uid-7", headers={"X-Service-Token": TOKEN})

    assert resp.status_code == 204
    assert deleted["uid"] == "uid-7"  # the legally-definitive step ran
    assert fake_service.storage_bucket().removed == [["uid-7.jpg"]]  # photo erased too
