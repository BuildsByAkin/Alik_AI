"""Part-1 scaffold API: open health, token-gated deletion seam. InMemoryStore + fake brain."""

from __future__ import annotations


def test_health_is_open(client) -> None:
    resp = client.get("/health")  # no token required
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_delete_requires_service_token(client) -> None:
    assert client.delete("/users/u1").status_code == 401  # no header


def test_delete_rejects_wrong_token(client) -> None:
    assert client.delete("/users/u1", headers={"X-Service-Token": "nope"}).status_code == 401


def test_delete_with_token_succeeds(client, headers) -> None:
    # No-op on the empty store for now, but the seam exists from day one and is idempotent.
    assert client.delete("/users/u1", headers=headers).status_code == 204
    assert client.delete("/users/u1", headers=headers).status_code == 204
