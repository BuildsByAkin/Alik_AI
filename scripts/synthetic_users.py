"""Longitudinal stress test: 4 synthetic users, one conversation/day for N days.

This is NOT a unit test — it is a manual quality-review tool. Two real models talk: a
cheap model plays each user from a persona prompt, and the companion responds as normal
(also the cheap model by default — pass --smart for Sonnet). After each conversation
end_session fires (extraction runs); after each simulated day the sleep pass + proactivity
engine run. Everything is exported to output/synthetic/ for review with read_report.py.

The fake user only ever sees the TEXT the companion outputs — never its internals —
exactly like a real user. Each fake user carries its own session-summary history so it
has continuity across days.

Run it manually (do not import this into the app):
  uv run python scripts/synthetic_users.py                  # 7 days, cheap model, clean up
  uv run python scripts/synthetic_users.py --days 3 --turns 4  # quick, cheapest smoke check
  uv run python scripts/synthetic_users.py --smart          # premium companion (Sonnet) prose
  uv run python scripts/synthetic_users.py --keep           # leave data in the DB
  uv run python scripts/synthetic_users.py --report-only    # re-export current DB state

COST: the companion turn is the expensive call (premium model + biggest, growing context),
~days*users*turns of them. Since this harness reviews the MEMORY SYSTEM and not the
companion's prose, it runs the companion on the CHEAP model by DEFAULT — that alone is the
biggest saving over the first version. The user turns and all nightly passes are cheap-model
already. Levers, cheapest first:
  * default (cheap companion)         — ~1/5 the per-companion-call cost of --smart
  * --days N / --turns K              — cost scales ~linearly with both
  * --smart                           — only when you specifically want to judge tone/writing
A full 7-day default run is a few hundred cheap-model calls; --days 3 --turns 4 is a fraction
of that. Use --smart sparingly.

# REVIEW CHECKLIST — read the exports and check:
# 1. Reflect-back: did it fire at natural moments or feel intrusive? Did the question
#    feel warm?
# 2. Trait quality: are the detected traits insightful or superficial? Any obviously
#    wrong inferences?
# 3. Temporal resolution: did David's contradictions resolve correctly? Check
#    graph_facts.json — only current truth should show valid_until=null.
# 4. Commitment lifecycle: did Maya's commitment churn produce sensible statuses? Any
#    pile-up of stale pending nodes?
# 5. Proactivity tone: read the opener messages — do they feel like care or
#    interrogation?
# 6. Memory drift: by the last day, does the reflection accurately capture who each
#    person is? Or has it drifted?
# 7. James cold-start: did the companion handle sparse input gracefully? Did it
#    over-assume from little signal?
# 8. Sara patterns: did it pick up Sunday anxiety and the sister relationship? Did
#    reflect-back surface these naturally?
# 9. Repetition: did the companion start repeating itself by day 5-6? Same questions,
#    same observations?
# 10. Lapsed path: James is the lapsed-user test — did the proactive opener reference
#     something real or feel generic?
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alik import proactivity, sleep_pass
from alik.companion import Companion
from alik.config import Settings
from alik.extraction import Extractor
from alik.llm import AnthropicLLM
from alik.memory.graph import GraphMemory
from alik.prompt import load_persona

logger = logging.getLogger("alik.synthetic")

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "output" / "synthetic"


# --- personas ---------------------------------------------------------------


@dataclass
class SimulatedUser:
    user_id: str
    name: str
    persona_prompt: str
    turns_per_session: int = 7
    current_day: int = 0
    session_history: list[str] = field(default_factory=list)  # past session summaries


def build_users() -> list[SimulatedUser]:
    return [
        SimulatedUser(
            user_id="synthetic-maya",
            name="Maya",
            turns_per_session=7,
            persona_prompt=(
                "You are Maya, 29, a graphic designer in Austin. You talk a lot and openly. "
                "You have lots of commitments you set for yourself but often change direction. "
                "You started pottery classes but are now thinking about switching to "
                "glassblowing. You mention your boyfriend Carlos occasionally. You're excited "
                "about a freelance project but stressed about the deadline. Each day vary what "
                "you lead with — sometimes work stress, sometimes Carlos, sometimes the classes. "
                "Occasionally contradict something you said before."
            ),
        ),
        SimulatedUser(
            user_id="synthetic-james",
            name="James",
            turns_per_session=5,
            persona_prompt=(
                "You are James, 34, an accountant in Seattle. You are quiet and reserved. You "
                "give short answers. You don't volunteer much. You take a few turns to warm up "
                "before saying anything personal. You have one thing you care about deeply: your "
                "weekly hiking group, but you rarely bring it up first. You mentioned once that "
                "you're thinking about asking someone out but haven't said more. Keep answers "
                "to 1-2 sentences."
            ),
        ),
        SimulatedUser(
            user_id="synthetic-sara",
            name="Sara",
            turns_per_session=6,
            persona_prompt=(
                "You are Sara, 26, a nurse in Chicago. You are emotionally expressive and "
                "self-aware. You notice your own patterns — you often say things like 'I always "
                "do this' or 'I noticed I feel better when...'. You have a close relationship "
                "with your sister Jen. Sunday nights make you anxious. You love trail running and "
                "talk about it often. You have an open commitment to run a 10k next month. Each "
                "day include at least one emotional observation about yourself."
            ),
        ),
        SimulatedUser(
            user_id="synthetic-david",
            name="David",
            turns_per_session=6,
            persona_prompt=(
                "You are David, 31, a software engineer in NYC. You are analytical and sometimes "
                "change your mind mid-conversation. You said you prefer working from home but now "
                "you're in the office more. You said you don't drink but mentioned having wine "
                "last weekend. You're training for a marathon but told the companion last week "
                "you hurt your knee. Actively contradict things you've said before — not "
                "dramatically, just naturally the way people evolve. If the companion reflects "
                "something back, sometimes correct it firmly."
            ),
        ),
    ]


# --- resilient LLM wrapper (still the REAL model; just retries 529s) ----------


class RetryLLM:
    """Wraps AnthropicLLM and retries transient overloaded/5xx errors so a ~400-call
    run doesn't die on a blip. Same real model underneath."""

    def __init__(self, inner: AnthropicLLM, *, retries: int = 6) -> None:
        self._inner = inner
        self._retries = retries

    @staticmethod
    def _transient(exc: Exception) -> bool:
        return getattr(exc, "status_code", None) in (429, 500, 502, 503, 529) or (
            "overload" in str(exc).lower()
        )

    async def complete(self, *, system: str, messages: Sequence[dict]) -> str:
        for attempt in range(self._retries):
            try:
                return await self._inner.complete(system=system, messages=messages)
            except Exception as exc:
                if not self._transient(exc) or attempt == self._retries - 1:
                    raise
                await asyncio.sleep(3 * (attempt + 1))
        raise RuntimeError("unreachable")

    async def stream_reply(self, *, system: str, messages: Sequence[dict]) -> AsyncIterator[str]:
        for attempt in range(self._retries):
            emitted = False
            try:
                async for delta in self._inner.stream_reply(system=system, messages=messages):
                    emitted = True
                    yield delta
                return
            except Exception as exc:
                if emitted or not self._transient(exc) or attempt == self._retries - 1:
                    raise
                await asyncio.sleep(3 * (attempt + 1))


# --- the conversation loop ---------------------------------------------------


@dataclass
class ConversationLog:
    user_id: str
    name: str
    day: int
    session_id: str
    opener: str | None
    reflect_back_fired: bool
    turns: list[dict]  # [{"role": "user"|"assistant", "content": str}]
    summary: str | None


def _persona_system(user: SimulatedUser, day: int) -> str:
    parts = [user.persona_prompt]
    if user.session_history:
        recent = "\n".join(f"- {s}" for s in user.session_history[-5:])
        parts.append(f"What you remember from earlier conversations with your companion:\n{recent}")
    parts.append(
        f"It is day {day}. Stay fully in character as {user.name}. Reply with ONE natural, "
        "first-person message — no stage directions, no quotation marks, no narrating. Talk "
        "like a real person texting their companion."
    )
    return "\n\n".join(parts)


def _user_view(turns: list[dict], seed: str) -> list[dict]:
    """Render the dialogue from the FAKE USER's point of view: the companion is the
    'user' they are replying to, and their own lines are the 'assistant'. Anthropic
    needs the list to start with a user turn, so prepend a neutral seed when needed."""
    msgs = [
        {"role": "user" if t["role"] == "assistant" else "assistant", "content": t["content"]}
        for t in turns
    ]
    if not msgs or msgs[0]["role"] != "user":
        msgs.insert(0, {"role": "user", "content": seed})
    return msgs


async def run_conversation(
    user: SimulatedUser,
    companion: Companion,
    memory: GraphMemory,
    llm_user,
    settings: Settings,
    day: int,
) -> ConversationLog:
    session_id = uuid4().hex
    turns: list[dict] = []

    # The companion may open proactively (delivered check-in) before the user speaks.
    opener = await companion.open_session(user.user_id, session_id)
    if opener:
        turns.append({"role": "assistant", "content": opener})

    seed = (
        f"You are {user.name}. It is day {day}. Begin or continue today's conversation with "
        "your companion, however feels natural — vary what you lead with."
    )
    persona_system = _persona_system(user, day)

    spoken = 0
    while spoken < user.turns_per_session:
        user_msg = (
            await llm_user.complete(system=persona_system, messages=_user_view(turns, seed))
        ).strip()
        if not user_msg:
            break
        turns.append({"role": "user", "content": user_msg})
        spoken += 1

        reply_chunks: list[str] = []
        async for delta in companion.respond(user.user_id, session_id, user_msg):
            reply_chunks.append(delta)
        turns.append({"role": "assistant", "content": "".join(reply_chunks)})

    reflect_back_fired = session_id in companion._rb_done
    summary = await companion.end_session(user.user_id, session_id)
    await companion.drain()  # wait for the background extraction this session kicked off
    if summary:
        user.session_history.append(summary)

    return ConversationLog(
        user_id=user.user_id,
        name=user.name,
        day=day,
        session_id=session_id,
        opener=opener,
        reflect_back_fired=reflect_back_fired,
        turns=turns,
        summary=summary,
    )


async def run_day(
    day: int,
    users: list[SimulatedUser],
    companion: Companion,
    memory: GraphMemory,
    llm_user,
    llm_cheap,
    settings: Settings,
) -> tuple[list[ConversationLog], dict[str, list[dict]]]:
    """Run all 4 conversations (sequentially, for readable logs), then the nightly sleep
    pass + proactivity engine. Returns (conversation logs, per-user sleep-pass records)."""
    print(f"\n{'=' * 70}\nDAY {day}\n{'=' * 70}")
    logs: list[ConversationLog] = []
    for user in users:
        try:
            log = await run_conversation(user, companion, memory, llm_user, settings, day)
            logs.append(log)
            opener_note = " (opened proactively)" if log.opener else ""
            rb_note = " [reflect-back]" if log.reflect_back_fired else ""
            print(f"  {user.name}: {len(log.turns)} turns{opener_note}{rb_note}")
        except Exception:
            logger.exception("conversation failed for %s on day %d", user.user_id, day)
            print(f"  {user.name}: FAILED (logged) — continuing")

    # Nightly passes over all active users.
    sleep_records: dict[str, list[dict]] = {}
    try:
        reports = await sleep_pass.run(memory, llm_cheap, settings)
        for r in reports:
            sleep_records.setdefault(r.user_id, []).append(
                {
                    "day": day,
                    "promoted": len(r.promoted),
                    "resolved": len(r.resolved),
                    "decayed_episodes": r.decayed_episodes,
                    "decayed_facts": r.decayed_facts,
                    "reflection_written": bool(r.reflection),
                    "traits_detected": r.traits_detected,
                    "commitments_ticked": r.commitments_ticked,
                }
            )
        detected = sum(len(r.traits_detected) for r in reports)
        consolidated = sum(r.traits_consolidated for r in reports)
        pruned = sum(r.traits_pruned for r in reports)
        ticked = sum(r.commitments_ticked for r in reports)
        print(
            f"  sleep pass: {len(reports)} users, {detected} traits detected, "
            f"{consolidated} consolidated, {pruned} pruned, {ticked} commitments -> due"
        )
    except Exception:
        logger.exception("sleep pass failed on day %d", day)

    try:
        report = await proactivity.run(memory, llm_cheap, settings)
        print(f"  proactivity: queued={report.queued or '{}'} skipped={report.skipped_existing}")
    except Exception:
        logger.exception("proactivity failed on day %d", day)

    return logs, sleep_records


# --- export ------------------------------------------------------------------


async def _dump_nodes(memory: GraphMemory, user_id: str, label: str) -> list[dict]:
    """Dump ALL nodes of a label for a user (including closed ones), full properties,
    so a reviewer can see temporal resolution — closed nodes carry valid_until."""
    if memory._graph is None:
        return []
    res = await memory._graph._graph.query(
        f"MATCH (n:{label} {{user_id: $u}}) RETURN n", {"u": user_id}
    )
    nodes = []
    for row in res.result_set:
        node = row[0]
        props = dict(getattr(node, "properties", {}) or {})
        nodes.append(props)
    return nodes


async def export_report(
    memory: GraphMemory,
    users: list[SimulatedUser],
    conversation_logs: dict[str, list[ConversationLog]],
    sleep_logs: dict[str, list[dict]],
    output_dir: Path,
) -> None:
    pool = memory._base._pool
    for user in users:
        uid = user.user_id
        udir = output_dir / uid
        udir.mkdir(parents=True, exist_ok=True)

        # conversations.jsonl — one session per line, with its turns.
        with (udir / "conversations.jsonl").open("w", encoding="utf-8") as fh:
            for log in conversation_logs.get(uid, []):
                fh.write(
                    json.dumps(
                        {
                            "user_id": log.user_id,
                            "name": log.name,
                            "day": log.day,
                            "session_id": log.session_id,
                            "opener": log.opener,
                            "reflect_back_fired": log.reflect_back_fired,
                            "summary": log.summary,
                            "turns": log.turns,
                        }
                    )
                    + "\n"
                )

        # Graph dumps (full property bags, including closed/resolved nodes).
        _write(udir / "graph_facts.json", await _dump_nodes(memory, uid, "Fact"))
        _write(udir / "graph_traits.json", await _dump_nodes(memory, uid, "InferredTrait"))
        _write(udir / "graph_commitments.json", await _dump_nodes(memory, uid, "Commitment"))
        _write(udir / "emotional_signals.json", await _dump_nodes(memory, uid, "EmotionalSignal"))

        # Postgres dumps.
        reflections = await pool.fetch(
            "SELECT content, generated_at FROM reflections WHERE user_id=$1 ORDER BY generated_at",
            uid,
        )
        _write(udir / "reflections.json", [dict(r) for r in reflections])
        checkins = await pool.fetch(
            "SELECT id, commitment_id, checkin_type, message_hint, created_at, delivered_at "
            "FROM pending_checkins WHERE user_id=$1 ORDER BY created_at",
            uid,
        )
        _write(udir / "checkins.json", [dict(c) for c in checkins])

        _write(udir / "sleep_pass_logs.json", sleep_logs.get(uid, []))
    print(f"\nExported {len(users)} users to {output_dir}")


def _write(path: Path, data) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str, ensure_ascii=False)


async def _reconstruct_logs_from_db(memory: GraphMemory, user: SimulatedUser) -> list[dict]:
    """--report-only: turn-by-turn buffers are ephemeral and gone, so reconstruct
    coarse per-session records from durable episodic summaries (no turns)."""
    rows = await memory._base._pool.fetch(
        "SELECT session_id, summary, created_at FROM episodic_memory "
        "WHERE user_id=$1 ORDER BY created_at",
        user.user_id,
    )
    return [
        {
            "user_id": user.user_id,
            "name": user.name,
            "day": i + 1,
            "session_id": r["session_id"],
            "opener": None,
            "reflect_back_fired": None,
            "summary": r["summary"],
            "turns": [],
        }
        for i, r in enumerate(rows)
    ]


# --- main --------------------------------------------------------------------


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


async def _run(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.WARNING)
    settings = Settings()
    memory = await _connect(settings)
    users = build_users()

    try:
        if args.report_only:
            logs = {u.user_id: await _reconstruct_logs_from_db(memory, u) for u in users}
            # _reconstruct returns dicts; wrap export to accept raw dicts.
            await _export_raw(memory, users, logs, {}, OUTPUT_DIR)
            print("Re-exported current DB state (transcripts unavailable in report-only mode).")
            return

        # COST: the companion turn is the priciest call (premium model + biggest, growing
        # context, ~days*users*turns of them). This harness reviews the MEMORY SYSTEM, not
        # the companion's prose, so default it to the cheap model. Pass --smart for a
        # full-quality (Sonnet) pass when you specifically want to judge tone/writing.
        companion_model = settings.companion_model if args.smart else settings.extraction_model
        print(
            f"companion model: {companion_model}" + ("" if args.smart else "  (--smart for Sonnet)")
        )
        llm_companion = RetryLLM(
            AnthropicLLM(
                api_key=settings.anthropic_api_key.get_secret_value(),
                model=companion_model,
                max_tokens=settings.companion_max_tokens,
            )
        )
        llm_user = RetryLLM(
            AnthropicLLM(
                api_key=settings.anthropic_api_key.get_secret_value(),
                model=settings.extraction_model,  # haiku plays all 4 users
                max_tokens=400,
            )
        )
        llm_cheap = RetryLLM(
            AnthropicLLM(
                api_key=settings.anthropic_api_key.get_secret_value(),
                model=settings.extraction_model,  # haiku for extraction + nightly passes
                max_tokens=settings.extraction_max_tokens,
            )
        )
        companion = Companion(
            memory=memory,
            llm=llm_companion,
            persona=load_persona(settings.persona_path),
            episode_limit=settings.episode_retrieve_limit,
            extractor=Extractor(llm=llm_cheap, memory=memory),
            reflect_back_min_turn=settings.reflect_back_min_turn,
            reflect_back_min_confidence=settings.reflect_back_min_confidence,
            reflect_back_confidence_bump=settings.reflect_back_confidence_bump,
            corrected_trait_confidence=settings.corrected_trait_confidence,
            reflect_back_cooldown_sessions=settings.reflect_back_cooldown_sessions,
        )

        # Optional per-session turn cap (fewer turns = fewer paid calls).
        if args.turns:
            for u in users:
                u.turns_per_session = min(u.turns_per_session, args.turns)

        # Fresh slate for the synthetic ids.
        for u in users:
            await memory.delete(u.user_id)

        all_logs: dict[str, list[ConversationLog]] = {u.user_id: [] for u in users}
        sleep_logs: dict[str, list[dict]] = {}
        started = datetime.now(UTC)
        for day in range(1, args.days + 1):
            day_logs, day_sleep = await run_day(
                day, users, companion, memory, llm_user, llm_cheap, settings
            )
            for log in day_logs:
                all_logs[log.user_id].append(log)
            for uid, records in day_sleep.items():
                sleep_logs.setdefault(uid, []).extend(records)

        await export_report(memory, users, all_logs, sleep_logs, OUTPUT_DIR)

        # Final summary.
        print(f"\n{'=' * 70}\nFINAL SUMMARY ({(datetime.now(UTC) - started).seconds}s)\n{'=' * 70}")
        total_turns = sum(len(log.turns) for logs in all_logs.values() for log in logs)
        print(f"total turns across all sessions: {total_turns}")
        for u in users:
            traits = await memory.get_current_traits(u.user_id)
            by_status: dict[str, int] = {}
            for t in traits:
                by_status[str(t.status)] = by_status.get(str(t.status), 0) + 1
            commitments = await _dump_nodes(memory, u.user_id, "Commitment")
            resolved = sum(
                1 for c in commitments if str(c.get("status", "")).startswith("resolved")
            )
            rb = sum(1 for log in all_logs[u.user_id] if log.reflect_back_fired)
            openers = sum(1 for log in all_logs[u.user_id] if log.opener)
            print(
                f"  {u.name}: traits={len(traits)} {by_status} | commitments_resolved={resolved} "
                f"| reflect_backs={rb} | proactive_openers={openers}"
            )
        print("\nReview with:  uv run python scripts/read_report.py --summary")

        if not args.keep:
            for u in users:
                await memory.delete(u.user_id)
            print("\nCleaned up all 4 synthetic users (use --keep to retain DB data).")
        else:
            print("\n--keep set: synthetic users left in the DB for inspection.")
    finally:
        await memory.aclose()


async def _export_raw(memory, users, logs_dicts, sleep_logs, output_dir) -> None:
    """Export path for --report-only, where logs are already plain dicts."""
    pool = memory._base._pool
    for user in users:
        uid = user.user_id
        udir = output_dir / uid
        udir.mkdir(parents=True, exist_ok=True)
        with (udir / "conversations.jsonl").open("w", encoding="utf-8") as fh:
            for rec in logs_dicts.get(uid, []):
                fh.write(json.dumps(rec) + "\n")
        _write(udir / "graph_facts.json", await _dump_nodes(memory, uid, "Fact"))
        _write(udir / "graph_traits.json", await _dump_nodes(memory, uid, "InferredTrait"))
        _write(udir / "graph_commitments.json", await _dump_nodes(memory, uid, "Commitment"))
        _write(udir / "emotional_signals.json", await _dump_nodes(memory, uid, "EmotionalSignal"))
        reflections = await pool.fetch(
            "SELECT content, generated_at FROM reflections WHERE user_id=$1 ORDER BY generated_at",
            uid,
        )
        _write(udir / "reflections.json", [dict(r) for r in reflections])
        checkins = await pool.fetch(
            "SELECT id, commitment_id, checkin_type, message_hint, created_at, delivered_at "
            "FROM pending_checkins WHERE user_id=$1 ORDER BY created_at",
            uid,
        )
        _write(udir / "checkins.json", [dict(c) for c in checkins])
        _write(udir / "sleep_pass_logs.json", sleep_logs.get(uid, []))
    print(f"Exported {len(users)} users to {output_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic longitudinal stress test.")
    parser.add_argument("--days", type=int, default=7, help="number of simulated days (default 7)")
    parser.add_argument(
        "--turns", type=int, default=0, help="cap user turns/session (0 = persona default ~6-8)"
    )
    parser.add_argument(
        "--smart",
        action="store_true",
        help="run the companion on the premium model (Sonnet) instead of the cheap default",
    )
    parser.add_argument("--keep", action="store_true", help="skip cleanup; leave data in the DB")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="skip simulation; just export current DB state for the 4 synthetic users",
    )
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
