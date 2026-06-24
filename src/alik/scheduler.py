"""APScheduler wrapper that runs the sleep pass nightly.

APScheduler is an OPTIONAL dependency. If it isn't installed, ``start_scheduler``
logs a warning and returns None — the app still runs, just without the cron.
Can also be run standalone: ``python -m alik.scheduler``.
"""

from __future__ import annotations

import asyncio
import logging

from alik import proactivity, sleep_pass
from alik.config import Settings
from alik.llm import LLMClient
from alik.memory.graph import GraphMemory

logger = logging.getLogger("alik.scheduler")

try:  # optional dependency
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:  # pragma: no cover - exercised only when dep is absent
    AsyncIOScheduler = None  # type: ignore[assignment,misc]
    CronTrigger = None  # type: ignore[assignment,misc]


def start_scheduler(memory: GraphMemory, llm: LLMClient, settings: Settings):
    """Start two jobs in one scheduler: the nightly sleep pass and the hourly
    proactivity engine. Returns the scheduler (so the caller can shut it down), or
    None if APScheduler is missing."""
    if AsyncIOScheduler is None:
        logger.warning("apscheduler not installed — scheduler (sleep pass + proactivity) disabled")
        return None
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        sleep_pass.run,
        CronTrigger(hour=settings.sleep_pass_hour, minute=0),
        args=[memory, llm, settings],
        id="sleep_pass",
        replace_existing=True,
    )
    scheduler.add_job(
        proactivity.run,
        CronTrigger(hour=f"*/{settings.proactivity_interval_hours}", minute=30),
        args=[memory, llm, settings],
        id="proactivity",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "scheduled: sleep pass daily at %02d:00, proactivity every %dh",
        settings.sleep_pass_hour,
        settings.proactivity_interval_hours,
    )
    return scheduler


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
        reflection_after_days=settings.reflection_after_days,
        confidence_decay_days=settings.confidence_decay_days,
        confidence_decay_factor=settings.confidence_decay_factor,
        confidence_floor=settings.confidence_floor,
    )
    llm = AnthropicLLM(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.extraction_model,
        max_tokens=settings.extraction_max_tokens,
    )
    scheduler = start_scheduler(memory, llm, settings)
    if scheduler is None:
        await memory.aclose()
        return
    try:
        await asyncio.Event().wait()  # run until interrupted
    finally:
        scheduler.shutdown(wait=False)
        await memory.aclose()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
