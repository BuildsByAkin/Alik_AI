"""HTTP surface. Every route here is service-to-service (token-gated). ``/health`` is the
only open route and lives in ``main`` outside this router.

Part 1 exposes just the deletion seam so right-to-erasure is wired from day one; the brain's
DELETE /users/{id} will be extended to call this in Part 5.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from connections_service.deps import verify_service_token
from connections_service.store import Store

router = APIRouter(dependencies=[Depends(verify_service_token)])


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, request: Request) -> None:
    """Erase this service's data for the user (cross-service account deletion)."""
    store: Store = request.app.state.store
    await store.delete_user(user_id)
