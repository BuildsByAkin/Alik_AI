"""The meet lifecycle: the advance pass (queues the next check-in) and the reply handlers
(advance state + write matchmaking memories to the brain).

Split of responsibility:
- ``advance_pass`` QUEUES the next nudge for each active meet (pref -> confirm -> followup),
  guarded by per-stage asked-flags so it's idempotent and safe to run on a frequent cron. It
  never sets an asked-flag unless the brain accepted the check-in (so a brain outage retries).
- ``apply_pref`` / ``apply_confirm`` / ``apply_followup`` advance the STATE when the companion
  posts a user's reply back, and write the social-event memories ("a meet is set", "met").

Everything about the OTHER person stays anonymous (``descriptor_for``) — never a name.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from rendezvous_service.models import Meet, MeetStatus
from rendezvous_service.store import Store

logger = logging.getLogger("rendezvous.lifecycle")


def _build_plan(meet: Meet) -> str:
    """A rough relayed plan from both free-text preferences (no parsing in the MVP)."""
    return f"{meet.pref_a} / {meet.pref_b}"


async def advance_pass(store: Store, brain_client) -> dict[str, int]:
    """Queue the next coordination check-in for every active meet. Never raises."""
    counts = {"meets": 0, "pref": 0, "confirm": 0, "followup": 0, "failures": 0}
    for meet in await store.get_active_meets():
        counts["meets"] += 1
        try:
            if meet.status is MeetStatus.COORDINATING:
                meet = await _ask_missing(store, brain_client, meet, "pref", counts)
            elif meet.status is MeetStatus.CONFIRMING:
                meet = await _ask_missing(store, brain_client, meet, "confirm", counts)
            elif meet.status is MeetStatus.CONFIRMED:
                meet = await _ask_missing(store, brain_client, meet, "followup", counts)
        except Exception:
            counts["failures"] += 1
            logger.exception("advance failed for meet %s", meet.id)
    logger.info(
        "PASS_SUMMARY pass=advance meets=%(meets)s pref=%(pref)s confirm=%(confirm)s "
        "followup=%(followup)s failures=%(failures)s",
        counts,
    )
    return counts


_STAGE_TYPE = {
    "pref": "rendezvous_pref",
    "confirm": "rendezvous_confirm",
    "followup": "rendezvous_followup",
}


def _reason(meet: Meet, user_id: str, stage: str) -> str:
    who = meet.descriptor_for(user_id)
    if stage == "pref":
        return f"help them settle on a rough area and time to meet {who}"
    if stage == "confirm":
        return f"a rough plan to meet {who}: {meet.plan}"
    return f"they were going to meet {who} — ask how it went"


async def _ask_missing(store: Store, brain_client, meet: Meet, stage: str, counts: dict) -> Meet:
    """Queue the ``stage`` check-in for each side that hasn't been asked yet; set the asked-flag
    only when the brain accepted it. Saves once."""
    changed = False
    for side, user in (("a", meet.user_a), ("b", meet.user_b)):
        if getattr(meet, f"{stage}_asked_{side}"):
            continue
        checkin_id = await brain_client.queue_checkin(
            user, _STAGE_TYPE[stage], _reason(meet, user, stage), meet_id=meet.id
        )
        if checkin_id is None:
            counts["failures"] += 1
            continue
        meet = replace(meet, **{f"{stage}_asked_{side}": True})
        counts[stage] += 1
        changed = True
    if changed:
        await store.save_meet(meet)
    return meet


# --- reply handlers (companion posts the user's reply back) ---------------------------------


async def apply_pref(store: Store, meet: Meet, user_id: str, text: str) -> Meet:
    """Record a side's rough where/when; when both are in, form a plan and move to CONFIRMING."""
    side = meet.side(user_id)
    if side is None:
        return meet
    meet = replace(meet, **{f"pref_{side}": text})
    if meet.both_prefs_in and meet.status is MeetStatus.COORDINATING:
        meet = replace(meet, plan=_build_plan(meet), status=MeetStatus.CONFIRMING)
    await store.save_meet(meet)
    return meet


async def apply_confirm(
    store: Store, brain_client, meet: Meet, user_id: str, accepted: bool
) -> Meet:
    """Record a yes/no to the plan. Any decline cancels; both-yes confirms + records the memory."""
    side = meet.side(user_id)
    if side is None:
        return meet
    meet = replace(meet, **{f"confirm_{side}": accepted})
    if meet.any_declined:
        meet = replace(meet, status=MeetStatus.CANCELLED)
    elif meet.both_confirmed:
        meet = replace(meet, status=MeetStatus.CONFIRMED)
        for u in (meet.user_a, meet.user_b):
            await brain_client.record_social_event(
                u,
                "meet_set",
                f"arranged to meet {meet.descriptor_for(u)} — {meet.plan}",
                counterpart_ref=meet.counterpart(u),
            )
    await store.save_meet(meet)
    return meet


async def apply_followup(
    store: Store, brain_client, meet: Meet, user_id: str, felt_positive: bool
) -> Meet:
    """Record how a meet felt for one side; write the 'met' memory; finish when both are in."""
    side = meet.side(user_id)
    if side is None:
        return meet
    meet = replace(meet, **{f"followup_{side}": felt_positive})
    feeling = "and it went well" if felt_positive else "though it was just okay"
    await brain_client.record_social_event(
        user_id,
        "met",
        f"met {meet.descriptor_for(user_id)} {feeling}",
        counterpart_ref=meet.counterpart(user_id),
    )
    if meet.both_followed_up:
        meet = replace(meet, status=MeetStatus.FOLLOWED_UP)
    await store.save_meet(meet)
    return meet


def main() -> None:
    """One-shot advance pass from the CLI (the `rendezvous-advance` console script)."""
    import asyncio

    from rendezvous_service.brain_client import BrainClient
    from rendezvous_service.config import settings
    from rendezvous_service.store import PgStore

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    async def _run() -> None:
        store = await PgStore.connect(settings.database_url)
        brain = BrainClient(
            base_url=settings.brain_url, service_token=settings.service_token.get_secret_value()
        )
        try:
            print(await advance_pass(store, brain))
        finally:
            await brain.aclose()
            await store.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
