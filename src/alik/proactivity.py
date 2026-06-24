"""The proactivity engine (Phase 5): decides who to check in with, when, about what.

Runs hourly (not just nightly). For each active user it walks a fixed priority order
— due commitment → commitment coming due → lapsed user → nothing — and QUEUES at most
one check-in. It never sends anything: the companion delivers the queued opener at the
next session start. Talks only to ``GraphMemory`` (no DB driver), and never raises per
user. Degrades gracefully: if the graph is down the commitment paths read empty and the
logic falls through to the Postgres-only general (lapsed) check-in.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from alik.config import Settings
from alik.llm import LLMClient
from alik.memory.graph import GraphMemory
from alik.models import CheckinType, PendingCheckin
from alik.prompt import (
    GENERAL_CHECKIN_SYSTEM,
    PROACTIVITY_SYSTEM,
    build_general_checkin_request,
    build_proactivity_request,
)

logger = logging.getLogger("alik.proactivity")


@dataclass
class ProactivityReport:
    queued: dict[str, int] = field(default_factory=dict)  # checkin_type -> count
    skipped_existing: int = 0  # users that already had an undelivered check-in
    considered: int = 0

    def _bump(self, checkin_type: CheckinType) -> None:
        self.queued[checkin_type] = self.queued.get(checkin_type, 0) + 1


async def decide_for_user(
    memory: GraphMemory, llm: LLMClient, user_id: str, settings: Settings
) -> CheckinType | None:
    """Queue at most one check-in for a user, in strict priority order. Returns the
    type queued, or None if nothing was warranted."""
    # One undelivered check-in per user at a time — never stack them up.
    if await memory.get_pending_checkin(user_id) is not None:
        return None

    # 1) A due commitment we haven't already asked about today.
    due = await memory.get_due_commitments(user_id)
    if due:
        commitment = due[0]
        facts = await memory.get_current_facts(user_id)
        hint = (
            await llm.complete(
                system=PROACTIVITY_SYSTEM,
                messages=build_proactivity_request(commitment, facts),
            )
        ).strip()
        if hint:
            await memory.queue_checkin(
                PendingCheckin(
                    user_id=user_id,
                    checkin_type=CheckinType.DUE_COMMITMENT,
                    message_hint=hint,
                    commitment_id=commitment.id,
                )
            )
            await memory.mark_commitment_reminded(commitment.id)
            return CheckinType.DUE_COMMITMENT

    # 2) A commitment coming due soon — a gentle heads-up.
    upcoming = await memory.get_upcoming_commitments(
        user_id, within_hours=settings.proactivity_upcoming_hours
    )
    if upcoming:
        commitment = upcoming[0]
        facts = await memory.get_current_facts(user_id)
        hint = (
            await llm.complete(
                system=PROACTIVITY_SYSTEM,
                messages=build_proactivity_request(commitment, facts),
            )
        ).strip()
        if hint:
            await memory.queue_checkin(
                PendingCheckin(
                    user_id=user_id,
                    checkin_type=CheckinType.UPCOMING_COMMITMENT,
                    message_hint=hint,
                    commitment_id=commitment.id,
                )
            )
            await memory.mark_commitment_reminded(commitment.id)
            return CheckinType.UPCOMING_COMMITMENT

    # 3) Lapsed user — a light "how are things?" (not about a commitment).
    last_seen = await memory.get_last_session_at(user_id)
    if last_seen is not None:
        lapsed = datetime.now(UTC) - last_seen > timedelta(days=settings.proactivity_lapsed_days)
        if lapsed:
            traits = await memory.get_current_traits(user_id)  # best-effort trait anchor
            hint = (
                await llm.complete(
                    system=GENERAL_CHECKIN_SYSTEM,
                    messages=build_general_checkin_request(traits),
                )
            ).strip()
            if hint:
                await memory.queue_checkin(
                    PendingCheckin(
                        user_id=user_id,
                        checkin_type=CheckinType.GENERAL_CHECKIN,
                        message_hint=hint,
                    )
                )
                return CheckinType.GENERAL_CHECKIN

    # 4) Nothing warranted today.
    return None


async def run(memory: GraphMemory, llm: LLMClient, settings: Settings) -> ProactivityReport:
    """Run the engine over every active user. Never raises."""
    report = ProactivityReport()
    try:
        users = await memory.get_active_users(within_days=settings.active_user_window_days)
    except Exception:
        logger.exception("proactivity: could not list active users")
        return report
    for user_id in users:
        report.considered += 1
        try:
            if await memory.get_pending_checkin(user_id) is not None:
                report.skipped_existing += 1
                continue
            queued = await decide_for_user(memory, llm, user_id, settings)
            if queued is not None:
                report._bump(queued)
        except Exception:
            logger.exception("proactivity failed for user %s", user_id)
    logger.info(
        "proactivity complete: considered=%d queued=%s skipped=%d",
        report.considered,
        report.queued,
        report.skipped_existing,
    )
    return report


async def _main() -> None:
    from alik.llm import AnthropicLLM

    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    memory = await GraphMemory.connect(
        database_url=settings.database_url,
        redis_url=settings.redis_url,
        falkordb_url=settings.falkordb_url,
        graph_name=settings.graph_name,
        working_ttl_seconds=settings.working_buffer_ttl_seconds,
        current_facts_limit=settings.current_facts_limit,
    )
    llm = AnthropicLLM(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.extraction_model,  # cheap model for the hourly hint
        max_tokens=settings.extraction_max_tokens,
    )
    try:
        report = await run(memory, llm, settings)
        print(
            f"considered={report.considered} queued={report.queued} "
            f"skipped_existing={report.skipped_existing}"
        )
    finally:
        await memory.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
