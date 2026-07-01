"""HTTP surface. Every route here is service-to-service (token-gated). ``/health`` is the
only open route and lives in ``main`` outside this router.

- DELETE /users/{id}: the right-to-erasure seam (called by the brain's delete fan-out).
- POST /matches/response: the companion's callback when a user responds to an introduction.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, status

from connections_service.deps import verify_service_token
from connections_service.models import (
    GroupResponse,
    GroupStatus,
    MatchResponse,
    MatchStatus,
    SharedInterests,
)
from connections_service.store import Store

router = APIRouter(dependencies=[Depends(verify_service_token)])


def _descriptor(shared: SharedInterests) -> str:
    """A privacy-safe, anonymized descriptor of the other person, from shared interests only —
    never a name. Used as what each side is told about the other when a meet is created."""
    labels = [n.canonical_label for n in shared.specific[:3]]
    if labels:
        return "someone who also loves " + ", ".join(labels)
    if shared.broad:
        pretty = [b.replace("_", " ") for b in shared.broad[:2]]
        return "someone who's also into " + ", ".join(pretty)
    return "someone alik thought you'd click with"


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, request: Request) -> None:
    """Erase this service's data for the user (cross-service account deletion)."""
    store: Store = request.app.state.store
    await store.delete_user(user_id)


@router.post("/matches/response", status_code=status.HTTP_204_NO_CONTENT)
async def match_response(body: MatchResponse, request: Request) -> None:
    """The companion closes the loop: the user said yes/no to an introduction. When BOTH sides
    have accepted each other, hand the pair to the rendezvous service to coordinate a meeting."""
    store: Store = request.app.state.store
    status_ = MatchStatus.ACCEPTED if body.accepted else MatchStatus.SKIPPED
    await store.update_match_status(body.user_id, body.candidate_id, status_, datetime.now(UTC))
    if not body.accepted:
        return
    rendezvous = getattr(request.app.state, "rendezvous_client", None)
    if rendezvous is None:
        return
    reverse = await store.get_match_state(body.candidate_id, body.user_id)
    if reverse is None or reverse.status is not MatchStatus.ACCEPTED:
        return  # the other side hasn't accepted (yet) — no meet
    shared = await store.get_shared_interests(body.user_id, body.candidate_id)
    desc = _descriptor(shared)  # same anonymized shared-interest descriptor for both sides
    await rendezvous.create_meet(body.user_id, body.candidate_id, desc, desc)


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
