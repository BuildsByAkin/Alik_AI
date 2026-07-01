# Phase 8 — Rendezvous (meeting coordination) + shared-memory write-back

**Status:** design / proposal (no code yet). This is the doc to react to before we build.

## 1. The gap this closes

Today the people-matching loop dead-ends the moment it succeeds. When a user says "yes, I'd
love to meet them," the companion literally replies *"you'll help make the introduction
happen — **no pressure, no logistics yet**"* (`companion._handle_match_response`) and then…
nothing. There is no step that actually gets two people into the same room, and the brain never
records that it introduced them at all — the match lives only in the connections service's own
Postgres.

So we have two distinct holes:

1. **No meeting lifecycle.** Match → "yes" → 🛑. Nobody collects *where/when*, proposes a plan,
   confirms it, or follows up on how it went.
2. **The brain doesn't remember its own matchmaking.** The companion can't later say *"did you
   ever meet that potter I mentioned?"* because the match was never written to memory. Each
   service is a silo; the brain — the thing the user actually talks to — is out of the loop.

Phase 8 closes both: a **rendezvous** service that owns the meeting lifecycle, and a
**memory write-back contract** so matching/connections/rendezvous all feed the shared brain —
"bringing the tasks into one body."

## 2. Design principles (inherited, non-negotiable)

- **The brain stays modality-independent** (text in, text out). Rendezvous never talks to the
  user directly — it queues a check-in with a directive and the companion delivers it, exactly
  like connections and matching already do. Voice (Phase 6) then works for free.
- **All memory access goes through the `Memory` interface**, and **`Memory.delete(user_id)` must
  FULLY erase a user** (legal, not a feature). This is the hardest constraint here because a meet
  involves *two* people — see §6.
- **Privacy: the counterpart's identity never leaks into the other user's world.** We already
  fought this (Bug 1 — the opener inventing a name). Rendezvous must keep the other person as
  "someone who loves pottery," never a name, until/unless a real hand-off is designed.
- **Build only this phase; leave clean seams.** Ship the MVP loop; don't pre-build scheduling
  integrations, maps, calendars, etc.

## 3. Shape: a new `services/rendezvous/` microservice

Mirrors the connections/matching pattern: own Postgres, own venv/`.env`, mesh-token guarded.

| | value |
|---|---|
| Path | `services/rendezvous/` |
| Port | 8004 |
| Datastore | Postgres 5435 (its own) |
| Talks to | brain (queue check-ins + memory write-back), connections (match-accept source) |
| LLM? | light — classify a free-text reply into a preference/confirmation (like connections eval) |

**Why a new service and not part of connections?** Connections' job is *who should meet*.
Rendezvous' job is *make the meeting happen and close the loop*. Different lifecycle, different
state, different cadence. Keeping them separate preserves the small-focused-module rule and lets
connections stay a pure scoring/surfacing engine.

## 4. The meeting lifecycle (state machine)

Rendezvous owns one row per accepted match (a "meet"), keyed by the (user_a, user_b) pair:

```
              connections: both sides accepted the intro
                              │
                              ▼
   ┌────────────┐  ask each side  ┌──────────────┐  both replied  ┌────────────┐
   │  PROPOSED  │────────────────▶│ COORDINATING │───────────────▶│  PROPOSED  │
   │  (created) │  "where/when     │ (collecting   │  a concrete    │   PLAN     │
   └────────────┘   works?"        │  preferences) │  place/time    └─────┬──────┘
                                   └──────────────┘                       │ both confirm
                                                                          ▼
   ┌───────────┐   after the date   ┌───────────┐   both said yes   ┌───────────┐
   │ FOLLOWED  │◀───────────────────│    MET     │◀─────────────────│ CONFIRMED │
   │   _UP     │  "how did it go?"  │ (happened) │                  │           │
   └───────────┘                    └───────────┘                   └───────────┘

   Any state ──(either side declines / goes cold N days)──▶ CANCELLED
```

- **PROPOSED → COORDINATING:** rendezvous wakes on an accepted match and queues each user a
  check-in: *"want to figure out a time/place to meet them?"* + collect a **rough area** and
  **availability window** (kept vague for privacy — "weekends, somewhere in Uptown," not an
  address).
- **COORDINATING → PROPOSED PLAN:** once both prefs are in, rendezvous computes a simple overlap
  (shared area + overlapping window) and proposes it back through the companion.
- **CONFIRMED:** both say the plan works.
- **MET → FOLLOWED_UP:** after the date, ask each side *how it felt* (not "did you go" — same tone
  rule as proactivity), and write the outcome to memory.
- **CANCELLED:** either side declines, or it goes cold (a staleness sink, like commitments).

MVP can collapse this to **COORDINATING → CONFIRMED → FOLLOWED_UP** (skip auto-proposing a plan;
just relay each side's suggestion) and grow the middle later.

## 5. The companion seam (no new pattern — reuse check-ins)

New `CheckinType`s in the brain (delivery/classification only; the state lives in rendezvous):

| CheckinType | Directive to the companion | Callback |
|---|---|---|
| `RENDEZVOUS_PREF` | "ask, warmly, roughly where/when they'd like to meet the person who loves X" | POST the free-text back → rendezvous parses it |
| `RENDEZVOUS_PLAN` | "suggest this rough plan and see if it works for them" | yes/no → rendezvous |
| `RENDEZVOUS_CONFIRM` | "let them know it's set — {area}, {window}" | ack |
| `RENDEZVOUS_FOLLOWUP` | "ask how it *felt*, not whether they went" | outcome → rendezvous + memory |

Each mirrors the existing `PEOPLE_MATCH` plumbing: rendezvous `queue_checkin` → companion
delivers at next `open_session` → companion classifies the reply → POSTs to rendezvous. The
brain keeps only the delivery/classification glue (as it does for jobs + people-matching).

## 6. The memory write-back contract (the "one body" part)

Every service writes back to the brain so the companion stays coherent — **per user, always
anonymized about the counterpart.** Proposed writes (all through the `Memory` interface):

| Event | Written to brain memory (for user A) | Type |
|---|---|---|
| connections surfaced an intro | "alik introduced them to someone who loves pottery" | episodic note |
| user accepted | "they were open to meeting the pottery person" | episodic / light fact |
| rendezvous meet confirmed | "a meet-up is set with the pottery person (Uptown, this weekend)" | commitment-like |
| meet followed up | how it *felt* → an `EmotionalSignal` (feeds the pattern layer, like job follow-through) | signal |

Matching (jobs) should do the same for symmetry: "alik recommended a barista role; they liked
it." Today none of this is written — closing that is half the value.

**Two mechanisms, pick per event:**
- **A) A new `Memory` method** — e.g. `record_social_event(user_id, kind, text, provenance)` that
  writes an episodic/graph node. Clean, explicit, easy to erase. Preferred for durable events.
- **B) Reuse existing seams** — the follow-up *feeling* is already an `EmotionalSignal`
  (Phase-5 follow-through pattern); the meet-being-set could be a `CommitmentNode`. Reuse where
  the semantics already fit; add A only where they don't.

## 7. The hard part: cross-user data + `delete()`

A meet is inherently about two people, but `Memory.delete(A)` must fully erase A. Rules:

1. **Store the meet per-user, not as one shared row.** Rendezvous keeps A's view and B's view;
   each references the other only by an opaque id + an anonymized descriptor ("the pottery
   person"), never the counterpart's name/contact.
2. **On `delete(A)`:** the brain's `delete` fan-out (already the cross-service coordinator) calls
   rendezvous `DELETE /users/A`. Rendezvous erases A's side and **anonymizes B's side to a
   tombstone** ("the person you were going to meet is no longer available") — B's row survives but
   carries nothing about A. Brain memory writes for A are per-user and erased by `Memory.delete`
   as normal.
3. **No un-erasable shared state.** If we ever exchange real contact details for a hand-off, that
   becomes its own consent-gated, separately-erasable record — explicitly **out of MVP scope.**

This is the part most worth getting right on paper before any code.

## 8. Suggested phasing

- **8.0 (MVP):** rendezvous service + Postgres; wake on connections accept; `RENDEZVOUS_PREF`
  (collect rough area/window) → relay to the other side → `RENDEZVOUS_CONFIRM`; `RENDEZVOUS_FOLLOWUP`
  writes an `EmotionalSignal`. Memory write-back for "introduced / accepted / met" via one new
  `Memory.record_social_event`. Cross-user delete handled per §7.
- **8.1:** auto-propose a plan from overlapping prefs (the PROPOSED-PLAN state); staleness/cancel
  sink; the digest/monitoring we just built, extended to rendezvous passes.
- **8.2 (separate, consent-gated):** real hand-off (contact exchange), if ever.

## 9. Decisions (locked)

1. **Separate service** — ✅ DECIDED. `services/rendezvous/`, own Postgres (5435), port 8004
   (per §3). Keeps connections a pure scoring/surfacing engine.
2. **Memory write-back mechanism** — ✅ DECIDED. Add a thin new `Memory.record_social_event`
   for durable social events (introduced / accepted / meet set), and reuse the existing
   `EmotionalSignal` seam for how a meet *felt* (like job follow-through).
3. **`where` privacy bar** — ✅ DECIDED for MVP: keep it **vague** — the MVP collects a free-text
   "rough where/when" and just **relays** it between the two sides (no parsing, no address). A
   dedicated LLM step to parse/negotiate a concrete plan from the two free-text preferences is a
   **fast-follow after the MVP loop works** (the `PROPOSED PLAN` state in §4 / phase 8.1), not now.
4. **Jobs-matching writes back too** — ✅ DECIDED: yes. Once `record_social_event` exists,
   matching writes "alik recommended {role}; they liked it" for symmetry, so the companion
   remembers job nudges as well as people intros.
5. **Ordering vs. Phase 6 (voice)** — recommendation stands: **Phase 8 before voice** (it makes
   the product actually get people to meet; voice works over it for free). Open for final confirm.

### Locked build order (8.0 MVP)
1. Brain: `Memory.record_social_event` (+ erased by `delete`) and the new `CheckinType`s
   (`RENDEZVOUS_PREF` / `RENDEZVOUS_CONFIRM` / `RENDEZVOUS_FOLLOWUP`) + companion delivery/classify.
2. `services/rendezvous/` scaffold (Postgres 5435, mesh-guarded, own `.env`), the meet state
   machine (collapsed MVP: COORDINATING → CONFIRMED → FOLLOWED_UP), wake on connections accept.
3. Free-text pref relay (no LLM parsing yet) → confirm → follow-up writes an `EmotionalSignal`.
4. Cross-user `delete(A)` fan-out: erase A's side, tombstone B's (§7). Memory write-backs for
   introduced/accepted/met via `record_social_event`.
5. Matching writes back the job symmetry event. Extend the digest/monitoring to rendezvous passes.

Fast-follows (8.1): the LLM plan-negotiation step (decision 3), a staleness/cancel sink, richer
lifecycle. Out of scope until asked (8.2): real contact hand-off (consent-gated).
