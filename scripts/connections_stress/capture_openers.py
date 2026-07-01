"""Step 3 of the dry run: capture the ACTUAL people-match opener the companion delivers.

After the connections chain queues people_match / people_match_group check-ins into the brain,
this opens a real session per matched user and records the opener text the companion generates —
the "warm friend, not an algorithm" tone test that can't be unit-tested convincingly.

get_pending_checkin returns the NEWEST undelivered check-in first, and the connections
check-ins were just created, so they deliver ahead of any leftover proactivity check-ins from
seeding. We loop per user (newest first) and stop at the first non-people check-in. Openers are
generated on the REAL companion model (Sonnet) because tone is exactly what we're judging.

  uv run python scripts/connections_stress/capture_openers.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import synthetic_users as su  # noqa: E402
from personas import IDENTITIES, user_ids  # noqa: E402

from alik.companion import Companion  # noqa: E402
from alik.config import Settings  # noqa: E402
from alik.extraction import Extractor  # noqa: E402
from alik.llm import AnthropicLLM  # noqa: E402
from alik.prompt import load_persona  # noqa: E402

OUT = (
    Path(__file__).resolve().parent.parent.parent
    / "output"
    / "connections_stress"
    / "_openers.json"
)
PEOPLE_TYPES = {"people_match", "people_match_group"}


async def _next_people_checkin(pool, user_id: str) -> dict | None:
    """Peek the newest undelivered check-in (same order open_session uses); None if not a
    people-match type (older seeding check-ins sort after the just-queued connections ones)."""
    row = await pool.fetchrow(
        "SELECT id, checkin_type, message_hint, payload FROM pending_checkins "
        "WHERE user_id = $1 AND delivered_at IS NULL ORDER BY created_at DESC LIMIT 1",
        user_id,
    )
    if row is None or row["checkin_type"] not in PEOPLE_TYPES:
        return None
    raw = row["payload"]
    return {
        "checkin_type": row["checkin_type"],
        "message_hint": row["message_hint"],
        "payload": json.loads(raw) if isinstance(raw, str) else raw,
    }


async def _run() -> None:
    logging.basicConfig(level=logging.WARNING)
    settings = Settings()
    memory = await su._connect(settings)
    # Real companion model — the opener tone is the whole point of this step.
    llm = su.RetryLLM(
        AnthropicLLM(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.companion_model,
            max_tokens=settings.companion_max_tokens,
        )
    )
    cheap = su.RetryLLM(
        AnthropicLLM(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.extraction_model,
            max_tokens=settings.extraction_max_tokens,
        )
    )
    companion = Companion(
        memory=memory,
        llm=llm,
        persona=load_persona(settings.persona_path),
        episode_limit=settings.episode_retrieve_limit,
        extractor=Extractor(llm=cheap, memory=memory),
        reflect_back_min_turn=settings.reflect_back_min_turn,
        reflect_back_min_confidence=settings.reflect_back_min_confidence,
        reflect_back_confidence_bump=settings.reflect_back_confidence_bump,
        corrected_trait_confidence=settings.corrected_trait_confidence,
        reflect_back_cooldown_sessions=settings.reflect_back_cooldown_sessions,
    )
    pool = memory._base._pool
    name = {uid: IDENTITIES[uid]["name"] for uid in IDENTITIES}

    results: dict[str, list[dict]] = {}
    try:
        print(f"companion model for openers: {settings.companion_model}")
        for uid in user_ids():
            captured: list[dict] = []
            for _ in range(4):  # a user may have a 1:1 AND a group check-in
                peek = await _next_people_checkin(pool, uid)
                if peek is None:
                    break
                opener = await companion.open_session(uid, uuid4().hex)
                # resolve candidate/group member names for the report
                payload = peek.get("payload") or {}
                cand = payload.get("candidate_id")
                # group payload carries the OTHER members under candidate_ids (not member_ids)
                members = payload.get("candidate_ids") or payload.get("member_ids") or []
                captured.append(
                    {
                        "checkin_type": peek["checkin_type"],
                        "candidate": name.get(cand, cand) if cand else None,
                        "group_members": [name.get(m, m) for m in members],
                        "payload": payload,
                        "opener": opener,
                    }
                )
            if captured:
                results[uid] = captured
                for c in captured:
                    who = c["candidate"] or ", ".join(c["group_members"])
                    print(f"  {name[uid]:5} <- {c['checkin_type']} ({who})")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, ensure_ascii=False)
        delivered = sum(len(v) for v in results.values())
        print(f"\ncaptured {delivered} openers across {len(results)} users -> {OUT}")
    finally:
        await memory.aclose()


if __name__ == "__main__":
    asyncio.run(_run())
