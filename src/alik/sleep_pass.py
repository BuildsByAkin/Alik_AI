"""The nightly sleep pass (Phases 3-5).

Runs, per active user, in order: PROMOTE → RESOLVE → DECAY → REFLECT → DETECT →
CONSOLIDATE (cross-key trait dedup) → PRUNE (close stale inferred traits) →
TICK_COMMITMENTS. Talks only to ``GraphMemory`` (the production composite), so it
touches no DB driver directly. It NEVER raises: each pass and each user is isolated,
errors are logged, and the run continues. Designed to be called by the scheduler or
run manually (``python -m alik.sleep_pass``, or ``--explain-trait <id>``).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from alik.config import Settings
from alik.job_matcher import Job, load_catalog, match_jobs_for_user
from alik.llm import LLMClient
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, InferredTrait, JobOutcome, PendingCheckin, TraitStatus
from alik.prompt import (
    COMMITMENT_CONSOLIDATE_SYSTEM,
    CONSOLIDATE_SYSTEM,
    DETECTION_SYSTEM,
    REFLECTION_SYSTEM,
    SALIENCE_SYSTEM,
    build_commitment_consolidation_request,
    build_consolidation_request,
    build_detection_request,
    build_reflection_request,
    build_salience_request,
    parse_consolidation,
    parse_detection,
    parse_index_groups,
    parse_salience,
)

logger = logging.getLogger("alik.sleep_pass")


@dataclass
class UserReport:
    user_id: str
    promoted: list[str] = field(default_factory=list)  # episode ids
    resolved: list[dict] = field(default_factory=list)  # contradiction audit rows
    decayed_episodes: int = 0
    decayed_facts: int = 0
    reflection: str | None = None
    traits_detected: list[str] = field(default_factory=list)  # trait keys landed this run
    traits_consolidated: int = 0  # cross-key duplicate traits merged this run
    traits_pruned: int = 0  # stale inferred traits closed this run
    commitments_consolidated: int = 0  # cross-key duplicate commitments merged this run
    commitments_ticked: int = 0  # commitments advanced pending -> due this run
    job_followups_queued: int = 0  # Phase 7: job follow-up check-ins queued this run
    jobs_matched: int = 0  # Phase 7: job recommendations queued this run


async def promote(
    memory: GraphMemory, llm: LLMClient, user_id: str, settings: Settings
) -> list[str]:
    """Score recent episodes for salience; promote those above the threshold."""
    candidates = await memory.get_recent_episodes(user_id, days=settings.promote_window_days)
    if not candidates:
        return []
    raw = await llm.complete(system=SALIENCE_SYSTEM, messages=build_salience_request(candidates))
    scores = parse_salience(raw, len(candidates))
    promoted: list[str] = []
    for episode, score in zip(candidates, scores, strict=True):
        if score > settings.promote_threshold and episode.id is not None:
            await memory.promote_episode(episode.id)
            promoted.append(episode.id)
    return promoted


async def resolve(memory: GraphMemory, user_id: str) -> list[dict]:
    """Close drifted duplicate current Facts; returns an audit row per closure."""
    return await memory.resolve_duplicate_facts(user_id)


async def decay(memory: GraphMemory, user_id: str, settings: Settings) -> tuple[int, int]:
    """Soft-delete old non-promoted episodes; decay stale fact confidence."""
    episodes = await memory.decay_episodes(user_id, older_than_days=settings.decay_after_days)
    facts = await memory.decay_stale_facts(user_id)
    return episodes, facts


async def reflect(
    memory: GraphMemory, llm: LLMClient, user_id: str, settings: Settings
) -> str | None:
    """Write and store a short reflection from the user's current picture."""
    facts = await memory.get_current_facts(user_id)
    commitments = await memory.get_open_commitments(user_id)
    signals = await memory.get_emotional_signals(user_id)
    promoted = await memory.get_promoted_episodes(user_id)
    if not (facts or commitments or signals or promoted):
        return None  # nothing to reflect on yet
    content = (
        await llm.complete(
            system=REFLECTION_SYSTEM,
            messages=build_reflection_request(facts, commitments, signals, promoted),
        )
    ).strip()
    if content:
        await memory.save_reflection(user_id, content)
    return content or None


async def detect(
    memory: GraphMemory, llm: LLMClient, user_id: str, settings: Settings
) -> list[InferredTrait]:
    """Fifth pass: infer durable patterns (InferredTraits) from the user's evidence.

    Reads promoted episodes + emotional signals, asks the cheap model for grounded
    patterns, validates provenance, and writes them with temporal resolution by key.
    Never raises: a bad detection yields no traits rather than crashing the pass.
    """
    episodes = await memory.get_promoted_episodes(user_id)
    signals = await memory.get_emotional_signals(user_id)
    if not (episodes or signals):
        return []
    # Feed back already-tracked patterns so the model reuses existing keys (idempotency).
    current_traits = await memory.get_current_traits(user_id)
    raw = await llm.complete(
        system=DETECTION_SYSTEM,
        messages=build_detection_request(episodes, signals, current_traits),
    )
    traits = parse_detection(
        raw,
        user_id=user_id,
        known_episode_ids={e.id for e in episodes if e.id},
        known_signal_ids={s.id for s in signals},
    )
    if traits:
        await memory.write_traits(traits)
    return traits


async def consolidate(memory: GraphMemory, llm: LLMClient, user_id: str, settings: Settings) -> int:
    """Cross-key semantic dedup of inferred traits (Phase 5.3). Asks the cheap model to
    group reworded duplicates that landed under different keys (string matching can't see
    meaning), then merges each group. Never touches confirmed traits. Returns merge count."""
    traits = [
        t for t in await memory.get_current_traits(user_id) if t.status is TraitStatus.INFERRED
    ]
    if len(traits) < 2:
        return 0
    raw = await llm.complete(
        system=CONSOLIDATE_SYSTEM, messages=build_consolidation_request(traits)
    )
    groups = parse_consolidation(raw, known_keys={t.key for t in traits})
    if not groups:
        return 0
    return await memory.consolidate_traits(user_id, groups)


async def consolidate_commitments(
    memory: GraphMemory, llm: LLMClient, user_id: str, settings: Settings
) -> int:
    """Cross-key semantic dedup of OPEN commitments (Phase 5.3) — the commitment mirror of
    consolidate(). A chatty user restates the same intent under fresh keys each session;
    same-key dedup and string matching miss it. Asks the cheap model to group duplicate
    open commitments (by index), then merges each group. Returns merge count."""
    openc = await memory.get_open_commitments(user_id)
    if len(openc) < 2:
        return 0
    raw = await llm.complete(
        system=COMMITMENT_CONSOLIDATE_SYSTEM,
        messages=build_commitment_consolidation_request(openc),
    )
    index_groups = parse_index_groups(raw, len(openc))
    if not index_groups:
        return 0
    id_groups = [[openc[i].id for i in grp] for grp in index_groups]
    return await memory.consolidate_commitments(user_id, id_groups)


async def tick_commitments(memory: GraphMemory, user_id: str, settings: Settings) -> int:
    """Sixth pass: advance pending commitments to DUE. NEVER resolves — only the user
    can do that via conversation.

    A commitment becomes due when its ``expected_by`` has passed, or — when no time was
    ever given — when it has sat in ``pending`` longer than the fallback window (it has
    been around long enough to be worth gently asking about). Returns the count marked.
    """
    pending = await memory.get_pending_commitments(user_id)
    if not pending:
        return 0
    now = datetime.now(UTC)
    fallback_cutoff = now - timedelta(days=settings.commitment_due_fallback_days)
    ticked = 0
    for c in pending:
        is_due = (c.expected_by is not None and c.expected_by <= now) or (
            c.expected_by is None and c.valid_from <= fallback_cutoff
        )
        if is_due:
            await memory.mark_commitment_due(c.id)
            ticked += 1
    return ticked


async def check_job_followups(memory: GraphMemory, catalog: list[Job], user_id: str) -> int:
    """Phase 7 (runs before match_jobs): if a delivered recommendation is past its 3-day
    follow_up_after with no follow-up yet, queue a ``job_followup`` check-in so the companion
    asks how it went. Respects the one-undelivered-check-in-per-user rule. Returns 0/1."""
    if await memory.get_pending_checkin(user_id) is not None:
        return 0
    rec = await memory.get_due_job_followup(user_id)
    if rec is None:
        return 0
    job = next((j for j in catalog if j.id == rec.job_id), None)
    title = job.title if job is not None else "that opportunity"
    partner = f" ({job.partner})" if job is not None else ""
    hint = (
        f"Check in warmly on how the '{title}' work{partner} went for them — ask how they "
        "FEEL about it, not just whether they did it."
    )
    await memory.queue_checkin(
        PendingCheckin(user_id=user_id, checkin_type=CheckinType.JOB_FOLLOWUP, message_hint=hint)
    )
    await memory.mark_job_followup_sent(rec.id)
    return 1


async def match_jobs(
    memory: GraphMemory, catalog: list[Job], user_id: str, settings: Settings
) -> int:
    """Phase 7: quietly surface relevant paid work from what the graph already knows.

    One open job thread per user: skips entirely while a recommendation is unresolved, or
    while in a post-outcome cooldown (disliked → 14d, not_tried → 7d). Never the same job
    twice; never a disliked partner again. Queues at most one ``job_recommendation`` check-in
    (and respects the one-check-in-per-user rule). Deterministic — no model call. Returns 0/1.
    """
    if await memory.get_pending_checkin(user_id) is not None:
        return 0

    recs = await memory.get_job_recommendations(user_id)  # newest first
    # An open thread (queued or awaiting an outcome) blocks any new recommendation.
    if any(r.outcome is None for r in recs):
        return 0

    now = datetime.now(UTC)
    if recs:
        latest = recs[0]  # most recent resolved thread
        # Cooldown anchored on when we asked (~ when the outcome was set); fall back to
        # the recommendation time if no follow-up was recorded.
        anchor = latest.follow_up_sent_at or latest.recommended_at
        if latest.outcome is JobOutcome.TRIED_DISLIKED and now - anchor < timedelta(
            days=settings.job_disliked_cooldown_days
        ):
            return 0
        if latest.outcome is JobOutcome.NOT_TRIED and now - anchor < timedelta(
            days=settings.job_not_tried_cooldown_days
        ):
            return 0

    # Never re-suggest a partner the user tried and disliked.
    disliked_job_ids = {r.job_id for r in recs if r.outcome is JobOutcome.TRIED_DISLIKED}
    by_id = {j.id: j for j in catalog}
    excluded_partners = {by_id[jid].partner for jid in disliked_job_ids if jid in by_id}

    facts = await memory.get_current_facts(user_id)
    traits = await memory.get_current_traits(user_id)  # match filters to CONFIRMED itself
    already = await memory.get_recommended_job_ids(user_id)

    job = match_jobs_for_user(
        user_id,
        facts,
        traits,
        catalog,
        already,
        threshold=settings.job_match_threshold,
        excluded_partners=excluded_partners,
    )
    if job is None:
        return 0

    hint = (
        f'You spotted some paid work that might suit them: "{job.title}" with {job.partner}, '
        f"paying {job.pay_range}. Mention it warmly, like a friend who noticed an opportunity — "
        f"not an ad — and offer to share the link: {job.partner_url}"
    )
    await memory.queue_checkin(
        PendingCheckin(
            user_id=user_id, checkin_type=CheckinType.JOB_RECOMMENDATION, message_hint=hint
        )
    )
    await memory.log_job_recommendation(
        user_id, job.id, follow_up_after_days=settings.job_followup_after_days
    )
    return 1


async def run_for_user(
    memory: GraphMemory,
    llm: LLMClient,
    user_id: str,
    settings: Settings,
    catalog: list[Job] | None = None,
) -> UserReport:
    """Run every pass for one user. Each pass is isolated; a failure is logged."""
    report = UserReport(user_id=user_id)
    try:
        report.promoted = await promote(memory, llm, user_id, settings)
    except Exception:
        logger.exception("PROMOTE failed for user %s", user_id)
    try:
        report.resolved = await resolve(memory, user_id)
    except Exception:
        logger.exception("RESOLVE failed for user %s", user_id)
    try:
        report.decayed_episodes, report.decayed_facts = await decay(memory, user_id, settings)
    except Exception:
        logger.exception("DECAY failed for user %s", user_id)
    try:
        report.reflection = await reflect(memory, llm, user_id, settings)
    except Exception:
        logger.exception("REFLECT failed for user %s", user_id)
    try:
        report.traits_detected = [t.key for t in await detect(memory, llm, user_id, settings)]
    except Exception:
        logger.exception("DETECT failed for user %s", user_id)
    try:
        report.traits_consolidated = await consolidate(memory, llm, user_id, settings)
    except Exception:
        logger.exception("CONSOLIDATE failed for user %s", user_id)
    try:
        report.traits_pruned = await memory.prune_stale_traits(
            user_id, stale_days=settings.trait_stale_days
        )
    except Exception:
        logger.exception("PRUNE_TRAITS failed for user %s", user_id)
    try:
        report.commitments_consolidated = await consolidate_commitments(
            memory, llm, user_id, settings
        )
    except Exception:
        logger.exception("CONSOLIDATE_COMMITMENTS failed for user %s", user_id)
    try:
        report.commitments_ticked = await tick_commitments(memory, user_id, settings)
    except Exception:
        logger.exception("TICK_COMMITMENTS failed for user %s", user_id)
    if catalog is not None:
        try:
            report.job_followups_queued = await check_job_followups(memory, catalog, user_id)
        except Exception:
            logger.exception("CHECK_JOB_FOLLOWUPS failed for user %s", user_id)
        try:
            report.jobs_matched = await match_jobs(memory, catalog, user_id, settings)
        except Exception:
            logger.exception("MATCH_JOBS failed for user %s", user_id)
    return report


async def run(memory: GraphMemory, llm: LLMClient, settings: Settings) -> list[UserReport]:
    """Run the sleep pass over every active user. Never raises."""
    try:
        users = await memory.get_active_users(within_days=settings.active_user_window_days)
    except Exception:
        logger.exception("sleep pass: could not list active users")
        return []
    # Load the job catalog once for the whole run; a bad/missing file disables the job
    # passes for this run rather than crashing it.
    catalog: list[Job] | None = None
    if settings.job_match_enabled:
        try:
            catalog = load_catalog(settings.job_catalog_path)
        except Exception:
            logger.exception("job catalog load failed — job passes disabled this run")
            catalog = None
    reports: list[UserReport] = []
    for user_id in users:
        try:
            reports.append(await run_for_user(memory, llm, user_id, settings, catalog))
        except Exception:
            logger.exception("sleep pass failed for user %s", user_id)
    logger.info("sleep pass complete: %d users processed", len(reports))
    return reports


async def _connect(settings: Settings) -> GraphMemory:
    return await GraphMemory.connect(
        database_url=settings.database_url,
        redis_url=settings.redis_url,
        falkordb_url=settings.falkordb_url,
        graph_name=settings.graph_name,
        working_ttl_seconds=settings.working_buffer_ttl_seconds,
        current_facts_limit=settings.current_facts_limit,
        reflection_after_days=settings.reflection_after_days,
        confidence_decay_days=settings.confidence_decay_days,
        confidence_decay_factor=settings.confidence_decay_factor,
        confidence_floor=settings.confidence_floor,
    )


async def explain_trait(memory: GraphMemory, trait_id: str) -> None:
    """Print exactly which episodes and signals produced a trait — full traceability.

    A wrong inference must be explainable without digging into the DB by hand.
    """
    trait = await memory.get_trait_by_id(trait_id)
    if trait is None:
        print(f"no trait with id {trait_id}")
        return
    episodes = {e.id: e.content for e in await memory.get_promoted_episodes(trait.user_id)}
    signals = {s.id: s.content for s in await memory.get_emotional_signals(trait.user_id)}
    print(f"trait {trait.id} (user {trait.user_id})")
    print(f"  key={trait.key} status={trait.status} confidence={trait.confidence:.2f}")
    print(f"  content: {trait.content}")
    print(f"  valid_from={trait.valid_from.isoformat()} valid_until={trait.valid_until}")
    print("  provenance episodes:")
    for eid in trait.provenance.episode_ids:
        print(f"    [ep:{eid}] {episodes.get(eid, '<not found / decayed>')}")
    print("  provenance signals:")
    for sid in trait.provenance.signal_ids:
        print(f"    [sig:{sid}] {signals.get(sid, '<not found>')}")


async def _main() -> None:
    from alik.llm import AnthropicLLM

    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    memory = await _connect(settings)

    # --explain-trait <id>: traceability path, no LLM needed.
    if "--explain-trait" in sys.argv:
        idx = sys.argv.index("--explain-trait")
        try:
            trait_id = sys.argv[idx + 1]
        except IndexError:
            print("usage: python -m alik.sleep_pass --explain-trait <trait_id>")
            await memory.aclose()
            return
        try:
            await explain_trait(memory, trait_id)
        finally:
            await memory.aclose()
        return

    llm = AnthropicLLM(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.extraction_model,
        max_tokens=settings.extraction_max_tokens,
    )
    try:
        reports = await run(memory, llm, settings)
        for r in reports:
            print(
                f"{r.user_id}: promoted={len(r.promoted)} resolved={len(r.resolved)} "
                f"decayed_episodes={r.decayed_episodes} decayed_facts={r.decayed_facts} "
                f"reflection={'yes' if r.reflection else 'no'} "
                f"traits={len(r.traits_detected)} consolidated={r.traits_consolidated} "
                f"pruned={r.traits_pruned} commitments_merged={r.commitments_consolidated} "
                f"commitments_due={r.commitments_ticked} "
                f"job_followups={r.job_followups_queued} jobs_matched={r.jobs_matched}"
            )
    finally:
        await memory.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
