"""HTTP surface. Every route here is service-to-service (token-gated). ``/health`` is the
only open route and lives in ``main`` outside this router.

- DELETE /users/{id}: the right-to-erasure seam (called by the brain's delete fan-out).
- POST /matches/response: the companion's callback when a user responds to an introduction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, status

from connections_service.deps import verify_service_token
from connections_service.models import GroupResponse, GroupStatus, MatchResponse, MatchStatus
from connections_service.store import Store

router = APIRouter(dependencies=[Depends(verify_service_token)])


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, request: Request) -> None:
    """Erase this service's data for the user (cross-service account deletion)."""
    store: Store = request.app.state.store
    await store.delete_user(user_id)


@router.post("/matches/response", status_code=status.HTTP_204_NO_CONTENT)
async def match_response(body: MatchResponse, request: Request) -> None:
    """The companion closes the loop: the user said yes/no to an introduction."""
    store: Store = request.app.state.store
    status_ = MatchStatus.ACCEPTED if body.accepted else MatchStatus.SKIPPED
    await store.update_match_status(body.user_id, body.candidate_id, status_, datetime.now(UTC))


@router.post("/matches/group-response", status_code=status.HTTP_204_NO_CONTENT)
async def group_response(body: GroupResponse, request: Request) -> None:
    """A group member responded. Default threshold: any one decline declines the whole group;
    an accept doesn't change status (the lifecycle has no terminal 'accepted')."""
    if body.accepted:
        return
    store: Store = request.app.state.store
    group = await store.get_group_candidate(body.group_id)
    if group is not None and group.status is not GroupStatus.DECLINED:
        await store.update_group_status(body.group_id, GroupStatus.DECLINED)
