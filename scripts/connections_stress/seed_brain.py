"""Step 1 of the connections end-to-end dry run: populate the BRAIN with real memories.

Drives the companion in-process for the 8 MN personas over N days (reusing the machinery in
scripts/synthetic_users.py), then LEAVES the data in the brain's DBs so the HTTP Profile API
can serve it to the connections chain. Everything runs on the cheap model — this exercises the
memory/extraction/profile pipeline, not companion prose.

Exports per-user brain dumps (facts, traits, dimensions, commitments, reflections, transcripts)
to output/connections_stress/<user_id>/ for the review report.

  uv run python scripts/connections_stress/seed_brain.py --days 5 --turns 6
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/ for synthetic_users

import synthetic_users as su  # noqa: E402
from personas import IDENTITIES, PERSONAS  # noqa: E402

from alik.companion import Companion  # noqa: E402
from alik.config import Settings  # noqa: E402
from alik.extraction import Extractor  # noqa: E402
from alik.llm import AnthropicLLM  # noqa: E402
from alik.prompt import load_persona  # noqa: E402

logger = logging.getLogger("connections.seed")

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "output" / "connections_stress"


def build_pool() -> list[su.SimulatedUser]:
    return [
        su.SimulatedUser(
            user_id=uid,
            name=IDENTITIES[uid]["name"],
            turns_per_session=turns,
            persona_prompt=prompt,
        )
        for uid, (turns, prompt) in PERSONAS.items()
    ]


async def _dump_dimensions(memory, users, output_dir: Path) -> None:
    """export_report doesn't cover profile dimensions — connections reads them, so dump too."""
    for user in users:
        dims = await memory.get_profile_dimensions(user.user_id)
        rows = [
            {
                "dimension": d.dimension,
                "value": d.value,
                "confidence": d.confidence,
                "status": str(d.status),
            }
            for d in dims
        ]
        su._write(output_dir / user.user_id / "profile_dimensions.json", rows)


async def _run(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.WARNING)
    settings = Settings()
    memory = await su._connect(settings)
    users = build_pool()
    for u in users:
        u.turns_per_session = (
            min(u.turns_per_session, args.turns) if args.turns else u.turns_per_session
        )

    # Everything cheap: this run reviews the memory/matching pipeline, not prose.
    cheap = settings.extraction_model
    print(f"seeding {len(users)} MN personas | model={cheap} | days={args.days}")
    llm_cheap = su.RetryLLM(
        AnthropicLLM(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=cheap,
            max_tokens=settings.extraction_max_tokens,
        )
    )
    llm_user = su.RetryLLM(
        AnthropicLLM(
            api_key=settings.anthropic_api_key.get_secret_value(), model=cheap, max_tokens=400
        )
    )
    companion = Companion(
        memory=memory,
        llm=llm_cheap,
        persona=load_persona(settings.persona_path),
        episode_limit=settings.episode_retrieve_limit,
        extractor=Extractor(llm=llm_cheap, memory=memory),
        reflect_back_min_turn=settings.reflect_back_min_turn,
        reflect_back_min_confidence=settings.reflect_back_min_confidence,
        reflect_back_confidence_bump=settings.reflect_back_confidence_bump,
        corrected_trait_confidence=settings.corrected_trait_confidence,
        reflect_back_cooldown_sessions=settings.reflect_back_cooldown_sessions,
    )

    try:
        for u in users:  # fresh slate for the synthetic ids
            await memory.delete(u.user_id)

        all_logs: dict[str, list] = {u.user_id: [] for u in users}
        sleep_logs: dict[str, list[dict]] = {}
        started = datetime.now(UTC)
        for day in range(1, args.days + 1):
            day_logs, day_sleep = await su.run_day(
                day, users, companion, memory, llm_user, llm_cheap, settings
            )
            for log in day_logs:
                all_logs[log.user_id].append(log)
            for uid, records in day_sleep.items():
                sleep_logs.setdefault(uid, []).extend(records)

        await su.export_report(memory, users, all_logs, sleep_logs, OUTPUT_DIR)
        await _dump_dimensions(memory, users, OUTPUT_DIR)

        print(f"\nseed complete in {(datetime.now(UTC) - started).seconds}s — data LEFT in the DB")
        for u in users:
            facts = await memory.get_current_facts(u.user_id)
            traits = await memory.get_current_traits(u.user_id)
            dims = await memory.get_profile_dimensions(u.user_id)
            print(
                f"  {u.name:5} ({u.user_id}): "
                f"facts={len(facts)} traits={len(traits)} dims={len(dims)}"
            )
    finally:
        await memory.aclose()


def main() -> None:
    p = argparse.ArgumentParser(description="Seed the brain for the connections dry run.")
    p.add_argument("--days", type=int, default=5)
    p.add_argument("--turns", type=int, default=6)
    asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    main()
