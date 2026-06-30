"""The surfacing pass: the step that makes a match reach a real person.

For each pool_ready user, take their top surfaceable match (one introduction at a time) and
queue a people-match opener in the brain; the companion delivers it next session. The
candidate is recorded as ``shown`` so it's never re-surfaced. Per-user isolated; never raises.
"""

from __future__ import annotations

import logging
import time

from connections_service.config import Settings
from connections_service.eval import _interest_label
from connections_service.models import MatchCheckin, MatchStateEntry, MatchStatus, SurfaceableMatch
from connections_service.passlog import format_pass_summary
from connections_service.store import Store, now_utc

logger = logging.getLogger("connections.surface")


def _shared_interest_labels(match: SurfaceableMatch, count: int) -> list[str]:
    """Top-N specific interest labels they share, or broad categories if none specific."""
    exp = match.explanation
    if exp.interest_specific:
        return [_interest_label(m.node_id) for m in exp.interest_specific[:count]]
    return [b.replace("_", " ") for b in exp.interest_broad[:count]]


async def surface_pass(store: Store, brain_client, settings: Settings) -> dict[str, int]:
    counts = {"users": 0, "surfaced": 0, "skipped": 0}
    # summary-only; brain_failures = queue_checkin returned None (a SUBSET of skipped). Other
    # surface exceptions stay in skipped + the logs, not here. counts dict unchanged for callers.
    brain_failures = 0
    start = time.perf_counter()
    try:
        for state in sorted(settings.launch_states_set):
            for entry in await store.get_pool_users(state):
                counts["users"] += 1
                try:
                    matches = await store.get_surfaceable_matches(
                        entry.user_id, state, surface_threshold=settings.surface_threshold
                    )
                except Exception:
                    logger.exception("surface: surfaceable read failed for %s", entry.user_id)
                    continue
                for match in matches[: settings.max_surface_per_pass]:
                    try:
                        if await _surface_one(store, brain_client, settings, match):
                            counts["surfaced"] += 1
                        else:
                            counts["skipped"] += 1
                            brain_failures += 1
                    except Exception:
                        logger.exception(
                            "surface failed for %s->%s", match.user_id_a, match.user_id_b
                        )
                        counts["skipped"] += 1
    finally:
        logger.info(
            format_pass_summary(
                "surface",
                checkins_queued=counts["surfaced"],
                brain_failures=brain_failures,
                duration_s=round(time.perf_counter() - start, 1),
            )
        )
    return counts


async def _surface_one(
    store: Store, brain_client, settings: Settings, match: SurfaceableMatch
) -> bool:
    checkin = MatchCheckin(
        candidate_id=match.user_id_b,
        reason=match.reason,
        shared_interests=_shared_interest_labels(match, settings.surface_shared_interests),
        match_confidence=match.final_confidence,
    )
    checkin_id = await brain_client.queue_checkin(match.user_id_a, checkin)
    if checkin_id is None:
        logger.warning(
            "surface: queue_checkin failed for %s->%s — retry next pass",
            match.user_id_a,
            match.user_id_b,
        )
        return False
    await store.save_match_state(
        MatchStateEntry(
            user_id=match.user_id_a,
            candidate_id=match.user_id_b,
            status=MatchStatus.SHOWN,
            checkin_id=checkin_id,
            surfaced_at=now_utc(),
        )
    )
    return True


def main() -> None:
    """One-shot surfacing from the CLI (the `connections-surface` console script)."""
    import asyncio

    from connections_service.brain_client import BrainClient
    from connections_service.config import settings
    from connections_service.store import PgStore

    async def _run() -> None:
        store = await PgStore.connect(settings.database_url)
        brain = BrainClient(
            base_url=settings.brain_url, service_token=settings.service_token.get_secret_value()
        )
        try:
            await surface_pass(store, brain, settings)
        finally:
            await brain.aclose()
            await store.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
