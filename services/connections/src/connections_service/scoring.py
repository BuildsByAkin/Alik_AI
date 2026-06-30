"""Candidate generation + the scoring run. Reads the store and calls the PURE kernel; the
kernel itself never touches the store. Runnable from APScheduler and the
``connections-score`` console script (mirrors ``connections-ingest``)."""

from __future__ import annotations

import logging
import time

from connections_service.config import Settings
from connections_service.kernel import MatchInput, kernel
from connections_service.models import CandidateScore
from connections_service.passlog import format_pass_summary
from connections_service.store import Store

logger = logging.getLogger("connections.scoring")


async def generate_candidates(
    store: Store, user_id: str, state: str, settings: Settings
) -> list[CandidateScore]:
    """Score every eligible pool_ready candidate for ``user_id`` and return the top N.

    Excludes self and already-shown users (the shown read is a Part-5 stub → []). O(N²) per
    run at the subject level — fine for the MN launch pool; see the README for the pre-filter
    seam (gate on broad-category overlap) if N grows large.
    """
    pool = await store.get_pool_users(state)
    shown = set(await store.get_shown_user_ids(user_id))
    subject = MatchInput(
        user_id,
        await store.get_user_interests(user_id),
        await store.get_profile_dimensions(user_id),
    )

    scored: list[CandidateScore] = []
    for entry in pool:
        if entry.user_id == user_id or entry.user_id in shown:
            continue
        candidate = MatchInput(
            entry.user_id,
            await store.get_user_interests(entry.user_id),
            await store.get_profile_dimensions(entry.user_id),
        )
        scored.append(kernel(subject, candidate, settings))

    scored.sort(key=lambda c: (c.score, c.user_id_b), reverse=True)
    return scored[: settings.top_n_candidates]


async def scoring_pass(store: Store, settings: Settings) -> dict[str, int]:
    """Re-score every pool_ready user (replacing prior scores). Per-user isolated; never raises."""
    counts = {"users": 0, "scored": 0}
    failures = 0  # summary-only; the returned counts dict stays stable for callers/tests.
    start = time.perf_counter()
    try:
        for state in sorted(settings.launch_states_set):
            for entry in await store.get_pool_users(state):
                try:
                    candidates = await generate_candidates(store, entry.user_id, state, settings)
                    for candidate in candidates:
                        await store.save_candidate_score(candidate)
                    counts["users"] += 1
                    counts["scored"] += len(candidates)
                except Exception:
                    failures += 1
                    logger.exception("connections scoring failed for user %s", entry.user_id)
    finally:
        logger.info(
            format_pass_summary(
                "score",
                pairs_scored=counts["scored"],
                users_processed=counts["users"] + failures,
                failures=failures,
                duration_s=round(time.perf_counter() - start, 1),
            )
        )
    return counts


def main() -> None:
    """One-shot scoring from the CLI (the `connections-score` console script)."""
    import asyncio

    from connections_service.config import settings
    from connections_service.store import PgStore

    async def _run() -> None:
        store = await PgStore.connect(settings.database_url)
        try:
            await scoring_pass(store, settings)
        finally:
            await store.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
