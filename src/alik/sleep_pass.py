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
from alik.llm import LLMClient
from alik.memory.graph import GraphMemory
from alik.models import InferredTrait, TraitStatus
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


async def run_for_user(
    memory: GraphMemory, llm: LLMClient, user_id: str, settings: Settings
) -> UserReport:
    """Run all six passes for one user. Each pass is isolated; a failure is logged."""
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
    return report


async def run(memory: GraphMemory, llm: LLMClient, settings: Settings) -> list[UserReport]:
    """Run the sleep pass over every active user. Never raises."""
    try:
        users = await memory.get_active_users(within_days=settings.active_user_window_days)
    except Exception:
        logger.exception("sleep pass: could not list active users")
        return []
    reports: list[UserReport] = []
    for user_id in users:
        try:
            reports.append(await run_for_user(memory, llm, user_id, settings))
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
                f"commitments_due={r.commitments_ticked}"
            )
    finally:
        await memory.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
