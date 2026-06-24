"""Human-friendly reader for the synthetic-run exports.

Reads the artifacts written by ``scripts/synthetic_users.py`` into
``output/synthetic/{user_id}/`` and prints them the way a person wants to read
them — transcripts day by day, traits with provenance, a one-page overview —
instead of digging through JSON.

Usage:
  uv run python scripts/read_report.py --user maya            # full 7-day transcript
  uv run python scripts/read_report.py --user maya --day 3    # just day 3
  uv run python scripts/read_report.py --user maya --traits   # traits + provenance + status
  uv run python scripts/read_report.py --user maya --facts    # current facts (valid_until=null)
  uv run python scripts/read_report.py --summary              # one-page overview of all 4 users

Friendly names (maya/james/sara/david) map to the synthetic-* user_ids; a full
user_id also works.
"""

from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNTH_DIR = REPO_ROOT / "output" / "synthetic"

NAME_TO_ID = {
    "maya": "synthetic-maya",
    "james": "synthetic-james",
    "sara": "synthetic-sara",
    "david": "synthetic-david",
}
ALL_USERS = list(NAME_TO_ID.values())
WIDTH = 88


# --- loading -----------------------------------------------------------------


def resolve_user(name: str) -> str:
    key = name.strip().lower()
    if key in NAME_TO_ID:
        return NAME_TO_ID[key]
    return key if key.startswith("synthetic-") else f"synthetic-{key}"


def _user_dir(user_id: str) -> Path:
    return SYNTH_DIR / user_id


def load_json(user_id: str, filename: str, default):
    path = _user_dir(user_id) / filename
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_jsonl(user_id: str, filename: str) -> list[dict]:
    path = _user_dir(user_id) / filename
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _missing(user_id: str) -> bool:
    return not _user_dir(user_id).exists()


# --- formatting --------------------------------------------------------------


def rule(char: str = "─") -> str:
    return char * WIDTH


def banner(text: str) -> str:
    return f"\n{rule('━')}\n{text}\n{rule('━')}"


def wrap(text: str, prefix: str) -> str:
    indent = " " * len(prefix)
    body = textwrap.fill(
        text.replace("\n", " ").strip(),
        width=WIDTH,
        initial_indent=prefix,
        subsequent_indent=indent,
    )
    return body


# --- commands ----------------------------------------------------------------


def print_transcript(user_id: str, only_day: int | None) -> None:
    sessions = load_jsonl(user_id, "conversations.jsonl")
    if not sessions:
        print(f"No conversations for {user_id}. Run synthetic_users.py first.")
        return
    sessions.sort(key=lambda s: (s.get("day", 0), s.get("session_id", "")))
    name = sessions[0].get("name", user_id)
    shown = 0
    print(banner(f"{name}  ({user_id})  —  conversation transcript"))
    for s in sessions:
        day = s.get("day")
        if only_day is not None and day != only_day:
            continue
        shown += 1
        print(f"\n{rule()}")
        print(f"DAY {day}    session {s.get('session_id', '?')[:8]}")
        print(rule())
        if s.get("opener"):
            print(wrap(s["opener"], "alik (proactive opener)> "))
            print()
        for turn in s.get("turns", []):
            speaker = "you> " if turn["role"] == "user" else "alik> "
            print(wrap(turn["content"], speaker))
        if s.get("reflect_back_fired"):
            print("\n   · [reflect-back fired this session]")
        if s.get("summary"):
            print()
            print(wrap(s["summary"], "summary> "))
    if shown == 0 and only_day is not None:
        print(f"\nNo session found for day {only_day}.")


def print_traits(user_id: str) -> None:
    traits = load_json(user_id, "graph_traits.json", [])
    if not traits:
        print(f"No traits for {user_id}.")
        return
    # Current (valid_until null) first, then closed; highest confidence first.
    traits.sort(key=lambda t: (t.get("valid_until") is not None, -float(t.get("confidence", 0))))
    print(banner(f"{user_id}  —  inferred traits ({len(traits)})"))
    for t in traits:
        live = "current" if t.get("valid_until") in (None, "") else "closed"
        print(
            f"\n[{t.get('status', '?')}/{live}]  {t.get('key', '?')}  "
            f"(confidence {float(t.get('confidence', 0)):.2f})"
        )
        print(wrap(t.get("content", ""), "  "))
        eps = t.get("provenance_episode_ids") or []
        sigs = t.get("provenance_signal_ids") or []
        print(f"  provenance: {len(eps)} episode(s), {len(sigs)} signal(s)")
        for e in eps:
            print(f"    ep:{e}")
        for sig in sigs:
            print(f"    sig:{sig}")
        if t.get("surfaced_in_session"):
            print(f"  surfaced in session: {t['surfaced_in_session'][:8]}")


def print_facts(user_id: str) -> None:
    facts = load_json(user_id, "graph_facts.json", [])
    current = [f for f in facts if f.get("valid_until") in (None, "")]
    if not current:
        print(f"No current facts for {user_id}.")
        return
    print(banner(f"{user_id}  —  current facts ({len(current)} of {len(facts)} total)"))
    for f in sorted(current, key=lambda x: x.get("key", "")):
        conf = float(f.get("confidence", 1.0))
        print(wrap(f"{f.get('content', '')}", f"  [{f.get('key', '?')}] "))
        print(f"      confidence {conf:.2f}")


def print_summary() -> None:
    print(banner("SYNTHETIC RUN — one-page overview"))
    any_data = False
    for user_id in ALL_USERS:
        if _missing(user_id):
            continue
        any_data = True
        sessions = load_jsonl(user_id, "conversations.jsonl")
        traits = load_json(user_id, "graph_traits.json", [])
        commitments = load_json(user_id, "graph_commitments.json", [])
        reflections = load_json(user_id, "reflections.json", [])
        name = sessions[0]["name"] if sessions else user_id

        current_traits = [t for t in traits if t.get("valid_until") in (None, "")]
        by_status: dict[str, int] = {}
        for t in current_traits:
            by_status[t.get("status", "?")] = by_status.get(t.get("status", "?"), 0) + 1
        commit_status: dict[str, int] = {}
        for c in commitments:
            commit_status[c.get("status", "?")] = commit_status.get(c.get("status", "?"), 0) + 1
        reflect_backs = sum(1 for s in sessions if s.get("reflect_back_fired"))
        openers = sum(1 for s in sessions if s.get("opener"))
        final_reflection = reflections[-1]["content"] if reflections else "(none)"

        print(f"\n{rule()}")
        print(f"{name}  ({user_id})    {len(sessions)} sessions")
        print(rule())
        trait_str = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "none"
        print(f"  traits (current):     {len(current_traits)}  [{trait_str}]")
        commit_str = ", ".join(f"{k}={v}" for k, v in sorted(commit_status.items())) or "none"
        print(f"  commitments:          {commit_str}")
        print(f"  reflect-backs fired:  {reflect_backs}")
        print(f"  proactive openers:    {openers}")
        print(wrap(final_reflection, "  final reflection:     "))
    if not any_data:
        print("\nNo synthetic data found. Run scripts/synthetic_users.py first.")


# --- entrypoint --------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Read synthetic-run exports, human-friendly.")
    parser.add_argument("--user", help="friendly name (maya/james/sara/david) or full user_id")
    parser.add_argument("--day", type=int, help="restrict transcript to a single day")
    parser.add_argument("--traits", action="store_true", help="print traits + provenance + status")
    parser.add_argument(
        "--facts", action="store_true", help="print current facts (valid_until=null)"
    )
    parser.add_argument("--summary", action="store_true", help="one-page overview of all 4 users")
    args = parser.parse_args()

    if args.summary:
        print_summary()
        return
    if not args.user:
        parser.error("provide --user NAME (or --summary)")

    user_id = resolve_user(args.user)
    if _missing(user_id):
        print(f"No export found for {user_id} at {_user_dir(user_id)}.")
        print("Run: uv run python scripts/synthetic_users.py")
        return

    if args.traits:
        print_traits(user_id)
    elif args.facts:
        print_facts(user_id)
    else:
        print_transcript(user_id, args.day)


if __name__ == "__main__":
    main()
