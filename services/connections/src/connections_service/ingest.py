"""The ingestion job: pull the roster from auth, fetch each rich profile from the brain,
derive a privacy-filtered snapshot + the interest graph, and upsert into our own Postgres.

Discipline (per the approved plan):
- One user's failure never aborts the run (isolated try/except, like the brain's sleep pass).
- A brain FETCH FAILURE (None) keeps the last snapshot and does NOT bump last_ingested_at —
  the brain is the system of record; a transient outage must never wipe/destale derived state.
- A successful-but-below-floor profile is upserted with pool_ready=false (a real signal).
- Never store raw sensitive content (relationship_goal / health_concern are not read;
  values_core only yields a derived cause node; dimension `content` is dropped).
"""

from __future__ import annotations

import logging
import time

from connections_service.config import Settings
from connections_service.interests import all_interest_nodes, extract_interests
from connections_service.models import DimensionSnapshot, UserPoolEntry
from connections_service.passlog import format_pass_summary
from connections_service.store import Store, now_utc

logger = logging.getLogger("connections.ingest")


async def run_ingest(store: Store, brain_client, auth_client, settings: Settings) -> dict[str, int]:
    """Ingest every launch-state roster. Returns counts; never raises."""
    counts = {"ingested": 0, "below_floor": 0, "skipped": 0}
    failures = 0  # summary-only; the returned counts dict stays stable for callers/tests.
    start = time.perf_counter()
    try:
        for state in sorted(settings.launch_states_set):
            user_ids = await auth_client.list_user_ids(state)
            for user_id in user_ids:
                try:
                    counts[await _ingest_one(user_id, store, brain_client, settings)] += 1
                except Exception:
                    failures += 1
                    logger.exception("connections ingest failed for user %s", user_id)
    finally:
        # pool_ready: count upserted as pool_ready THIS run (renamed from the spec's
        # `pool_ready_new` — we don't diff prior state, so it's a per-run count, not a delta).
        logger.info(
            format_pass_summary(
                "ingest",
                users_processed=sum(counts.values()) + failures,
                failures=failures,
                pool_ready=counts["ingested"],
                duration_s=round(time.perf_counter() - start, 1),
            )
        )
    return counts


async def _ingest_one(user_id: str, store: Store, brain_client, settings: Settings) -> str:
    profile = await brain_client.fetch_profile(user_id)
    if profile is None:
        # Transport/5xx failure — keep the existing snapshot, retry next cycle.
        logger.warning("connections ingest: brain fetch failed for %s — keeping snapshot", user_id)
        return "skipped"

    identity = profile.get("identity") or {}
    state = str(identity.get("state") or "").strip().upper()
    edges = extract_interests(profile, trait_confidence_floor=settings.trait_confidence_floor)
    dims = [
        DimensionSnapshot(
            dimension=str(d.get("dimension")),
            value=str(d.get("value")),
            confidence=_as_float(d.get("confidence")),
            status=str(d.get("status", "")),
        )
        for d in profile.get("dimensions", [])
    ]
    has_strong_dim = any(d.confidence >= settings.dimension_confidence_floor for d in dims)
    pool_ready = bool(state in settings.launch_states_set and (edges or has_strong_dim))

    entry = UserPoolEntry(
        user_id=user_id,
        state=state,
        age=identity.get("age"),
        city=identity.get("city"),
        pool_ready=pool_ready,
        last_ingested_at=now_utc(),
    )
    await store.upsert_user_pool(entry)
    await store.upsert_profile_dimensions(user_id, dims)
    await store.upsert_user_interests(user_id, edges)
    return "ingested" if pool_ready else "below_floor"


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    """One-shot ingest from the CLI (the `connections-ingest` console script)."""
    import asyncio

    from connections_service.auth_client import AuthClient
    from connections_service.brain_client import BrainClient
    from connections_service.config import settings
    from connections_service.store import PgStore

    async def _run() -> None:
        token = settings.service_token.get_secret_value()
        store = await PgStore.connect(settings.database_url)
        await store.ensure_interest_nodes(all_interest_nodes())
        brain = BrainClient(base_url=settings.brain_url, service_token=token)
        auth = AuthClient(base_url=settings.auth_url, service_token=token)
        try:
            await run_ingest(store, brain, auth, settings)
        finally:
            await brain.aclose()
            await auth.aclose()
            await store.aclose()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
