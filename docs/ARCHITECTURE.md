# alik — system architecture (briefing for the match-service planner)

This document describes what exists today so a planning agent can design the **match
service** (people-matching) on top of it. It covers the three live systems — **memory**,
**auth**, and the **companion** — and ends with *exactly what a match service can read about
a user* and *the conventions/seams to reuse*.

> One-line mental model: a **companion** talks to the user and learns them; a **memory**
> layer stores that learning; **auth** holds identity; small **microservices** (matching for
> jobs, and the future people-matching) are pure consumers that read an assembled **profile**
> over HTTP and never touch the brain's databases.

## Topology

| Service | Code | Port | Datastore | Talks to |
|---|---|---|---|---|
| **Companion (the "brain")** | `src/alik/` | 8000 | Postgres 5432, Redis 6379, FalkorDB 6380 (in-process) | auth, matching |
| **Auth + profile (identity)** | `services/auth/` | 8001 | Supabase (own project) | — |
| **Job matching** | `services/matching/` | 8002 | Postgres 5433 | brain (Profile API) |
| **People matching** | *to be built* | *8003?* | *its own Postgres* | brain (Profile API), auth |

All service-to-service calls carry a shared **`X-Service-Token`** (one secret, same value in
every service: brain `ALIK_SERVICE_TOKEN` == auth/matching `SERVICE_TOKEN`). See
`GOING_LIVE.md` for the operational checklist.

---

## 1. Memory system (inside the brain)

The golden rule: **all memory access goes through the `Memory` interface; nothing else
imports a DB driver.** That interface is the seam everything is built on.

### Layers

```
Companion / sleep pass / API
        │  (depends only on the Memory ABC)
   GraphMemory                      ← composes the two below; degrades gracefully if graph down
     ├── PgRedisMemory  (Postgres + Redis)   ← the ONLY importer of asyncpg / redis
     └── GraphStore     (FalkorDB)           ← the ONLY importer of the graph driver
```

- **`Memory` ABC** (`src/alik/memory/base.py`) — write / retrieve / invalidate / **delete** +
  episodic lifecycle + proactive check-in queue + reflect-back cooldown + profile dimensions.
- **`GraphMemory`** (`src/alik/memory/graph.py`) — wraps a base `Memory` and a `GraphStore`,
  adds the temporal graph, and is what the app actually uses. Reads degrade to empty/no-op
  when FalkorDB is down (the companion keeps working on episodic memory alone) — **except
  `delete()`, which is loud** (raises rather than half-erasing; legal right-to-erasure).

### What is stored, and where

| Knowledge | Type (`models.py`) | Store | Notes |
|---|---|---|---|
| Live session turns | `MemoryRecord` (WORKING) | **Redis** | hot buffer, TTL ~6h |
| Per-session summaries | `MemoryRecord` (EPISODIC) | **Postgres** `episodic_memory` | promoted/decayed by the sleep pass |
| Daily reflection | text | **Postgres** `reflections` | replaces episodes for 30+ day users |
| Durable facts | `GraphNode` (FACT) | **FalkorDB** | supersede-by-key; canonical key list (below) |
| Emotional signals | `GraphNode` (EMOTIONAL_SIGNAL) | **FalkorDB** | append-only time-series |
| Commitments | `CommitmentNode` | **FalkorDB** | lifecycle pending→due→resolved_kept/dropped |
| Inferred traits | `InferredTrait` | **FalkorDB** | free-form patterns; status inferred→confirmed→corrected |
| **Behavioral dimensions** | `ProfileDimension` | **Postgres** `profile_dimensions` | the "living profile"; fixed taxonomy |
| Proactive check-ins | `PendingCheckin` | **Postgres** `pending_checkins` | how a service reaches the user (see §3) |

### The living profile (behavioral layer) — most relevant to matching

A fixed taxonomy (`src/alik/profile.py`, `TAXONOMY`) of behavioral axes built **silently** by
the nightly `profile_pass`, then optionally **soft-confirmed** in conversation. Each is a
`ProfileDimension` with `value` + `confidence` + `status` (unconfirmed | confirmed | corrected)
+ `observation_count`, grounded in provenance.

| Axis | Values | Captures |
|---|---|---|
| `detail_specificity` | vague / moderate / highly_specific | how concrete they are about interests |
| `topic_focus` | deep_narrow / balanced / broad_shallow | go deep on one thing vs skim many |
| `interest_intensity` | casual / engaged / intense_specific | casual vs intense/specific interests |
| `structure_preference` | flexible / mixed / needs_structure | wants the plan in advance |
| `sensory_sensitivity` | low / medium / high | finds environments overwhelming |
| `social_predictability_need` | low / medium / high | needs to know who/what before social |

Add an axis/value by editing `TAXONOMY` — nothing else changes (detection prompt, validation,
behavior all read that table).

### Facts: the canonical key list

Extraction tags durable facts with canonical keys, so matching can rely on stable keys.
Categories (full list in `EXTRACTION_SYSTEM`, `src/alik/prompt.py`):
- **Lifestyle**: primary/secondary/tertiary_hobby, primary_exercise, sleep_pattern,
  diet_preference, music_taste, book_genre, food_cuisine_preference, sports_team, …
- **Work/ambition**: occupation, company, career_stage, ambition_level, skill_learning, …
- **Life situation**: location_city, living_situation, relationship_status,
  **relationship_goal**, family_situation, wants_children, pet, health_concern, …
- **Personality/values**: personality_trait, social_preference, introvert_extrovert,
  love_language, political_leaning, religious_belief, **values_core**, life_goal,
  energy_source, …

### The nightly sleep pass

Per active user, in order: promote → resolve → decay → reflect → **detect** (traits) →
consolidate → prune → consolidate_commitments → tick (commitments) → **profile_pass**
(dimensions). Runs in the companion process via APScheduler; also runnable standalone
(`uv run alik-sleep`).

---

## 2. Auth + profile service (`services/auth/`, :8001)

Standalone, **Supabase-backed** (its own project: Auth + a `profiles` table + Storage). The
single Supabase seam is `supabase_client.py`; nothing else imports the SDK.

### `profiles` table (identity)

| Field | Notes |
|---|---|
| `id` | = Supabase auth user id (uuid). **This is the `user_id` the brain/memory key on.** |
| `name`, `age`, `city` | age gated `>= 25` |
| **`state`** | 2-letter US code; **launch gate** (`LAUNCH_STATES`, currently `{"MN"}`) + matching pool |
| `photo_url` | public URL in the `profile-photos` bucket |
| `created_at`, `updated_at` | |

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/signup` | — | `{email,password,name,age,city,state}` → tokens. age<25 or unlaunched state → **403** |
| POST | `/auth/login` / `/refresh` | — | session tokens |
| POST | `/auth/logout` | Bearer | |
| DELETE | `/auth/account` | Bearer | hard-erase self (loud) |
| GET / PATCH | `/profile/me` | Bearer | read / edit (name, city only) |
| POST | `/profile/me/photo` | Bearer | upload jpeg/png ≤5MB |
| **GET** | **`/internal/profiles/{user_id}`** | **X-Service-Token** | identity by id (service-to-service) |
| **DELETE** | **`/internal/users/{user_id}`** | **X-Service-Token** | hard-erase by id (cross-service delete) |

Identity = `user_id`, name, age, city, **state**, photo_url. Tokens are validated by Supabase
(`auth.get_user`); we never decode JWTs ourselves.

---

## 3. Companion service (the brain, `src/alik/`, :8000)

The companion is **modality-independent (text in, text out)** and depends only on the `Memory`
interface + an LLM client — so swapping infra or (later) voice I/O is an injection change.

### What it does each turn (`companion.py`)
1. Write the user turn to the working buffer.
2. `retrieve()` the context (`RetrievedContext`: episodes/reflection, facts, open commitments,
   confirmed traits, **behavioral dimensions**).
3. Build the system prompt (`prompt.build_system_prompt`): persona + reflection + confirmed
   traits + current facts + open commitments + recent episodes + **behavior directives** (from
   confident dimensions, never labeling) + any proactive opener.
4. Optionally weave in **one gentle check** per session — a trait reflect-back **or** a profile
   soft-confirm (shared cadence cooldown).
5. Stream the reply; write the assistant turn.

After a session ends, **extraction** (`extraction.py`, cheap model) mines the transcript into
facts/signals/commitments asynchronously. **Proactivity** (`proactivity.py`) queues at most one
`pending_checkin` per user (due commitment → upcoming → lapsed → job rec/follow-up).

### API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/chat` | streamed reply (`{user_id, session_id, message}`) |
| POST | `/sessions/{id}/end` | summarize → episodic memory; fire extraction |
| **GET** | **`/users/{id}/profile`** | **the assembled living profile (see §4)** — service-token-guarded |
| DELETE | `/users/{id}` | **cross-service erasure**: brain memory → auth → matching |

Runtime models: companion `claude-sonnet-4-6`, extraction `claude-haiku-4-5` (both env-configurable;
never hardcoded). The brain holds an `auth_client` and a `matching_client`.

### How a service reaches the user
The user only talks to the companion. A backend service surfaces something by **queuing a
`PendingCheckin`**; the companion delivers it as the next session's opener and drives the reply
(this is exactly how job recommendations work — see `sleep_pass.match_jobs` + the companion's
job-delivery methods). **The people-match service can reuse this pattern** to introduce a match.

---

## 4. What a match service can read about a user — the Profile API

`GET /users/{user_id}/profile` on the brain (send `X-Service-Token`) returns the single rich
picture, assembled on read. This is the seam the job-matching service already uses; the people
matcher should use it too.

```jsonc
{
  "user_id": "…",
  "identity": {                       // from the auth service (null if auth unavailable)
    "id": "…", "name": "…", "age": 31,
    "city": "Minneapolis", "state": "MN", "photo_url": "…"
  },
  "facts": [                          // current durable facts (canonical keys)
    {"key": "relationship_goal", "content": "wants something long-term"},
    {"key": "primary_hobby", "content": "rock climbing on weekends"}
  ],
  "confirmed_traits": [              // user-confirmed personality patterns (free-form)
    {"key": "energized_by_deep_talks", "content": "…", "confidence": 0.9}
  ],
  "dimensions": [                   // the behavioral taxonomy (see §1)
    {"dimension": "interest_intensity", "value": "intense_specific",
     "content": "intensely into chess specifically", "confidence": 0.82,
     "status": "confirmed", "observation_count": 4}
  ]
}
```

So a match has, per user: **identity (incl. `state` for the pool)** + **facts** (interests,
relationship_goal, values, location) + **confirmed traits** + **behavioral dimensions**. That's
the "rich picture beyond surface interests" the matching was always meant to use.

---

## 5. Building the match service — conventions & seams to reuse

Mirror `services/matching/` (the closest precedent — read it first):

- **Standalone FastAPI**, own venv/`pyproject`, **own Postgres** (add a `matchN-postgres` to
  `docker-compose.yml` on a fresh host port), `schema.sql` auto-applied on connect.
- **`Store` ABC + `PgStore` + `InMemoryStore`** so the API + logic are testable with no infra.
- **`deps.verify_service_token`** gates every route with the shared `X-Service-Token`.
- **`brain_client`** reads the Profile API for each candidate (degrade to empty on failure).
- **`create_app(*, store=…, brain_client=…)`** injectable for tests; `main.py` builds real
  ones from `Settings` at startup.
- **No LLM needed** if scoring is deterministic (matching has none). Add one only if match
  reasoning needs it.
- **Reach the user through the companion** by queuing a `PendingCheckin` (don't build a second
  user-facing channel).
- **Join the deletion fan-out**: add a `DELETE /users/{id}` to the new service and call it from
  the brain's `DELETE /users/{id}` (right-to-erasure must stay complete).

### Open questions the planner will need to decide (not yet designed)
- **Pool & gating**: matches are within `state` (and presumably age/relationship_goal filters) —
  confirm the hard filters vs. soft scoring signals.
- **Mutuality & consent**: one-way suggestion vs. mutual opt-in; how/when a match is revealed;
  what the other person sees (the profile is sensitive — likely a curated subset, not raw facts).
- **Where match state lives**: candidate scores, shown/skipped/accepted, cooldowns → the new
  service's own Postgres (like the recommendation log).
- **Scoring inputs**: which facts/traits/dimensions matter, and how identity (`state`, age) hard-
  filters vs. behavioral compatibility soft-scores.
- **Real-life intro flow**: "introduces you to compatible people in real life" (per CLAUDE.md) —
  how that's mediated (through the companion conversation?) and what location/safety checks apply.

> Authoritative details + rationale for everything above live in `CLAUDE.md` (Key decisions).
> Per-service specifics are in each service's `README.md`. Deployment is in `GOING_LIVE.md`.
