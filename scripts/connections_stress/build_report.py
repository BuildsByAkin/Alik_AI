"""Assemble the human-readable review report from all the dumps produced by the dry run.

Stdlib only. Reads:
  output/connections_stress/<user_id>/{graph_facts,graph_traits,profile_dimensions}.json
  output/connections_stress/<user_id>/conversations.jsonl
  output/connections_stress/_connections/*.json   (the connections tables)
  output/connections_stress/_openers.json          (the delivered openers)

Writes output/connections_stress/REPORT.md — transcripts summary, what the brain remembered,
the derived interest graph, kernel scores, LLM eval reasons, what surfaced, groups, and the
actual openers, plus an automated sanity assessment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from personas import IDENTITIES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent / "output" / "connections_stress"
CONN = ROOT / "_connections"
NAME = {uid: ident["name"] for uid, ident in IDENTITIES.items()}


def _load(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def n(uid) -> str:
    return NAME.get(uid, uid)


def _current(nodes: list[dict]) -> list[dict]:
    return [x for x in nodes if not x.get("valid_until")]


def section_memory(lines: list[str]) -> None:
    lines.append("## 1. What the brain remembered (per user)\n")
    for uid in IDENTITIES:
        udir = ROOT / uid
        facts = _current(_load(udir / "graph_facts.json", []))
        traits = _current(_load(udir / "graph_traits.json", []))
        dims = _load(udir / "profile_dimensions.json", [])
        convos = _load_jsonl(udir / "conversations.jsonl")
        ident = IDENTITIES[uid]
        lines.append(f"### {n(uid)} — {ident['age']}, {ident['city']} ({uid})")
        lines.append(f"- sessions: {len(convos)}")
        if facts:
            lines.append("- **facts:**")
            for f in sorted(facts, key=lambda x: x.get("key", "")):
                lines.append(f"    - `{f.get('key')}` — {f.get('content')}")
        conf = [t for t in traits if str(t.get("status")) == "confirmed"]
        inf = [t for t in traits if str(t.get("status")) != "confirmed"]
        if conf:
            lines.append("- **confirmed traits:**")
            for t in conf:
                lines.append(f"    - {t.get('content')}  _(conf {t.get('confidence')})_")
        if inf:
            lines.append(
                f"- **inferred traits ({len(inf)}):** "
                + "; ".join(f"{t.get('content')} ({t.get('confidence')})" for t in inf[:6])
            )
        if dims:
            lines.append(
                "- **dimensions:** "
                + ", ".join(
                    f"{d['dimension']}={d['value']} ({d['confidence']}, {d['status']})"
                    for d in dims
                )
            )
        lines.append("")


def section_pool_and_interests(lines: list[str]) -> None:
    pool = _load(CONN / "users_pool.json", [])
    interests = _load(CONN / "user_interests.json", [])
    by_user: dict[str, list[dict]] = {}
    for e in interests:
        by_user.setdefault(e["user_id"], []).append(e)

    lines.append("## 2. Connections ingest — pool readiness + derived interest graph\n")
    lines.append("| User | City | pool_ready | interest edges (weight) |")
    lines.append("|---|---|---|---|")
    for p in sorted(pool, key=lambda x: x["user_id"]):
        edges = sorted(by_user.get(p["user_id"], []), key=lambda e: -float(e["weight"]))
        rendered = (
            ", ".join(f"{e['interest_node_id']}={round(float(e['weight']), 2)}" for e in edges)
            or "—"
        )
        lines.append(f"| {n(p['user_id'])} | {p.get('city')} | {p['pool_ready']} | {rendered} |")
    lines.append("")


def section_scores(lines: list[str]) -> None:
    scores = _load(CONN / "candidate_scores.json", [])
    lines.append("## 3. Kernel scores (directed A→B, top pairs)\n")
    lines.append("| A | B | score | interest | dim | values | conf | review? |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in sorted(scores, key=lambda x: -float(x["score"]))[:24]:
        lines.append(
            f"| {n(s['user_id_a'])} | {n(s['user_id_b'])} | {round(float(s['score']), 3)} "
            f"| {round(float(s['interest_score']), 2)} | {round(float(s['dimension_score']), 2)} "
            f"| {round(float(s['values_score']), 2)} | {round(float(s['confidence']), 2)} "
            f"| {'⚑' if s['human_review_flag'] else ''} |"
        )
    lines.append("")


def section_eval(lines: list[str]) -> None:
    evals = _load(CONN / "eval_results.json", [])
    lines.append("## 4. LLM cross-eval — would they click, and why\n")
    lines.append(
        "_This is the tone/quality test: read the `reason` strings as if alik is "
        "introducing two friends._\n"
    )
    for e in sorted(evals, key=lambda x: -float(x["final_confidence"])):
        click = "✅ would click" if e["would_click"] else "❌ pass"
        flag = f"  ⚑ REVIEW: {e.get('flag_reason')}" if e.get("flag_for_review") else ""
        llm_c = round(float(e["llm_confidence"]), 2)
        final_c = round(float(e["final_confidence"]), 2)
        lines.append(
            f"- **{n(e['user_id_a'])} → {n(e['user_id_b'])}** — {click} "
            f"(llm {llm_c}, final {final_c}){flag}"
        )
        lines.append(f"    > {e.get('reason')}")
    lines.append("")


def section_surface_and_groups(lines: list[str]) -> None:
    matches = _load(CONN / "match_state.json", [])
    groups = _load(CONN / "group_candidates.json", [])
    lines.append("## 5. What surfaced (1:1) + groups\n")
    if matches:
        lines.append("**1:1 introductions queued to the companion:**")
        for m in matches:
            lines.append(
                f"- {n(m['user_id'])} ← introduced to {n(m['candidate_id'])} "
                f"(status: {m['status']})"
            )
    else:
        lines.append("_No 1:1 matches surfaced._")
    lines.append("")
    if groups:
        lines.append("**Group candidates:**")
        for g in groups:
            members = ", ".join(n(m) for m in g["member_ids"])
            lines.append(
                f"- interest `{g['interest_node_id']}` — {members} "
                f"(mean score {round(float(g['mean_score']), 3)}, status {g['status']})"
            )
    else:
        lines.append("_No groups formed._")
    lines.append("")


def section_openers(lines: list[str]) -> None:
    openers = _load(ROOT / "_openers.json", {})
    lines.append("## 6. The payoff — actual openers the user would hear\n")
    if not openers:
        lines.append("_No openers captured._\n")
        return
    for uid, items in openers.items():
        for c in items:
            who = c.get("candidate") or ", ".join(c.get("group_members", []))
            kind = "group" if c["checkin_type"] == "people_match_group" else "1:1"
            lines.append(f"### To {n(uid)} — {kind} intro ({who})")
            lines.append(f"> {c.get('opener')}\n")


def assessment_checks() -> list[str]:
    pool = _load(CONN / "users_pool.json", [])
    scores = _load(CONN / "candidate_scores.json", [])
    evals = _load(CONN / "eval_results.json", [])
    matches = _load(CONN / "match_state.json", [])
    groups = _load(CONN / "group_candidates.json", [])
    counts = _load(CONN / "_row_counts.json", {})
    ready = [p for p in pool if p.get("pool_ready")]
    clicked = [e for e in evals if e["would_click"]]
    flagged = [e for e in evals if e.get("flag_for_review")]

    checks = [
        f"- Pool-ready users: **{len(ready)}/{len(IDENTITIES)}**",
        f"- Directed kernel pairs scored: **{len(scores)}**",
        f"- Cross-eval verdicts: **{len(evals)}** "
        f"({len(clicked)} would-click, {len(flagged)} flagged for review)",
        f"- 1:1 introductions surfaced: **{len(matches)}**",
        f"- Group candidates: **{len(groups)}**",
        f"- Connections table row counts: `{counts}`",
    ]
    # Wildcard sanity: Hank (chess/cooking/wine) should mostly NOT be a confident would-click.
    hank_clicks = [e for e in clicked if "cx-hank" in (e["user_id_a"], e["user_id_b"])]
    checks.append(
        f"- Wildcard control (Hank): {len(hank_clicks)} would-click verdicts involving Hank "
        f"(expected LOW — he shares little with the pool)"
    )
    return checks


def main() -> None:
    lines: list[str] = []
    lines.append("# Connections service — live end-to-end dry run\n")
    lines.append(
        "Synthetic Minnesota pool → real brain memory → real connections chain "
        "(ingest→score→eval→surface→cluster) → actual companion openers. Only the auth "
        "*data store* is faked (no Supabase); every other component runs for real.\n"
    )
    lines.append("## 0. Automated sanity checks\n")
    lines.extend(assessment_checks())
    lines.append("")
    section_memory(lines)
    section_pool_and_interests(lines)
    section_scores(lines)
    section_eval(lines)
    section_surface_and_groups(lines)
    section_openers(lines)

    out = ROOT / "REPORT.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
