# Going live — operational checklist

Read this top to bottom when you're ready to deploy. alik is **five services** that talk to
each other over HTTP:

| Service | Path | Port | Datastore |
|---|---|---|---|
| Brain (companion + memory + living profile) | `src/alik/` | 8000 | Postgres 5432, Redis 6379, FalkorDB 6380 |
| Auth + profile (identity) | `services/auth/` | 8001 | Supabase (its own project) |
| Job matching | `services/matching/` | 8002 | Postgres 5433 |
| Connections (people matching) | `services/connections/` | 8003 | Postgres 5434 |
| Rendezvous (meeting coordination) | `services/rendezvous/` | 8004 | Postgres 5435 |

> Rule of thumb: each service has its **own venv and its own `.env`**. Run `uv sync` inside
> each service directory before starting it.

---

## 0. One-time prerequisites
- [ ] `uv` installed, Docker running.
- [ ] A Supabase project created (for the auth service) — see step 3.
- [ ] Generate ONE shared mesh secret and keep it handy:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
  This same value goes in **all five** services (see step 2). Call it `<MESH_TOKEN>` below.

---

## 1. Infrastructure (Postgres × 4, Redis, FalkorDB)
- [ ] Start the datastores:
  ```bash
  docker compose up -d
  ```
- [ ] Confirm they're all healthy:
  ```bash
  docker compose ps        # postgres, redis, falkordb, matching/connections/rendezvous-postgres "healthy"
  ```
  > Schemas are created automatically on first connect (each service runs its own
  > `schema.sql`) — you do **not** run migrations by hand for the brain or matching.
  > (Supabase is the exception — see step 3.)

---

## 2. Secrets & environment

The mesh secret must be **identical** in every service, or service-to-service calls
return 401:

| Service | Variable | Value |
|---|---|---|
| Brain | `ALIK_SERVICE_TOKEN` | `<MESH_TOKEN>` |
| Auth | `SERVICE_TOKEN` | `<MESH_TOKEN>` |
| Matching | `SERVICE_TOKEN` | `<MESH_TOKEN>` |
| Connections | `SERVICE_TOKEN` | `<MESH_TOKEN>` |
| Rendezvous | `SERVICE_TOKEN` | `<MESH_TOKEN>` |

### Brain — `.env` (prefix `ALIK_`)
- [ ] `ALIK_ANTHROPIC_API_KEY` — **required** (the companion can't run without it).
- [ ] `ALIK_DATABASE_URL` — e.g. `postgresql://alik:alik@localhost:5432/alik`
- [ ] `ALIK_REDIS_URL` — e.g. `redis://localhost:6379/0`
- [ ] `ALIK_FALKORDB_URL` — e.g. `redis://localhost:6380/0`
- [ ] `ALIK_SERVICE_TOKEN` — `<MESH_TOKEN>`
- [ ] `ALIK_AUTH_SERVICE_URL` — e.g. `http://localhost:8001`
- [ ] `ALIK_MATCHING_SERVICE_URL` — e.g. `http://localhost:8002` (leave **empty to disable job
      matching** entirely — everything else still works)
- [ ] `ALIK_CONNECTIONS_SERVICE_URL` — e.g. `http://localhost:8003` (leave **empty to disable
      people matching**; the brain's delete fan-out + checkin queueing then skip it)
- [ ] `ALIK_RENDEZVOUS_SERVICE_URL` — e.g. `http://localhost:8004` (leave **empty to disable
      meeting coordination**; the companion then never routes rendezvous replies and the delete
      fan-out skips it)
- [ ] (optional) `ALIK_COMPANION_MODEL`, `ALIK_EXTRACTION_MODEL`, `ALIK_PERSONA_PATH`

### Auth — `services/auth/.env` (`cp .env.example .env`)
- [ ] `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_KEY`
- [ ] `SERVICE_TOKEN` — `<MESH_TOKEN>`
- [ ] `PORT=8001`

### Matching — `services/matching/.env` (`cp .env.example .env`)
- [ ] `DATABASE_URL` — `postgresql://alik:alik@localhost:5433/matching`
- [ ] `BRAIN_URL` — `http://localhost:8000`
- [ ] `SERVICE_TOKEN` — `<MESH_TOKEN>`
- [ ] `PORT=8002`

### Connections — `services/connections/.env` (`cp .env.example .env`)
- [ ] `DATABASE_URL` — `postgresql://alik:alik@localhost:5434/connections`
- [ ] `BRAIN_URL` — `http://localhost:8000` (reads the Profile API + queues check-ins)
- [ ] `AUTH_URL` — `http://localhost:8001` (the ingest roster — who exists, by state)
- [ ] `SERVICE_TOKEN` — `<MESH_TOKEN>`
- [ ] `ANTHROPIC_API_KEY` — **required for `eval_pass`** (its own key; confirm billing scope —
      separate from the brain's `ALIK_ANTHROPIC_API_KEY`)
- [ ] `RENDEZVOUS_URL` — e.g. `http://localhost:8004` (where connections creates a meet when two
      people mutually accept; empty disables the hand-off)
- [ ] `PORT=8003`
- [ ] (optional) `EVAL_MODEL`, the five `*_CRON` schedules, `MIN/MAX_GROUP_SIZE`, thresholds

### Rendezvous — `services/rendezvous/.env` (`cp .env.example .env`)
- [ ] `DATABASE_URL` — `postgresql://alik:alik@localhost:5435/rendezvous`
- [ ] `BRAIN_URL` — `http://localhost:8000` (queues coordination check-ins + records social events)
- [ ] `SERVICE_TOKEN` — `<MESH_TOKEN>`
- [ ] `PORT=8004`
- [ ] (optional) `ADVANCE_CRON` (default every 30 min — drives the meet lifecycle)

---

## 3. Supabase setup (auth service only, one-time)
- [ ] In the Supabase dashboard: **Auth → Providers → Email → uncheck "Confirm email"**
      (so signup returns a live session immediately).
- [ ] Run the schema SQL from `services/auth/README.md` ("Schema SQL") in the SQL editor —
      creates the `profiles` table (incl. the **`state`** column), RLS policies, and the
      `profile-photos` storage bucket.
- [ ] **Already created `profiles` before the `state` column existed?** Run the `ALTER TABLE`
      migration from `services/auth/README.md` ("Already created `profiles`?") to add `state`
      (the launch gate + the connections roster both depend on it).

---

## 4. Install each service
- [ ] `uv sync` (repo root — the brain)
- [ ] `cd services/auth && uv sync`
- [ ] `cd services/matching && uv sync`
- [ ] `cd services/connections && uv sync`
- [ ] `cd services/rendezvous && uv sync`

---

## 5. Start the services
Bring up all five (order isn't strict — cross-service calls are made lazily and each service
degrades gracefully if its peers are briefly unreachable):
- [ ] Brain: `uv run uvicorn alik.api:app --port 8000`
- [ ] Auth: `cd services/auth && uv run uvicorn auth_service.main:app --port 8001`
- [ ] Matching: `cd services/matching && uv run uvicorn matching_service.main:app --port 8002`
- [ ] Connections: `cd services/connections && uv run uvicorn connections_service.main:app --port 8003`
- [ ] Rendezvous: `cd services/rendezvous && uv run uvicorn rendezvous_service.main:app --port 8004`

### Smoke test
- [ ] `curl localhost:8000/...`  (brain has no /health; hit `/chat` or check logs start clean)
- [ ] `curl localhost:8001/health`  → `{"status":"ok"}`
- [ ] `curl localhost:8002/health`  → `{"status":"ok"}`
- [ ] `curl localhost:8003/health`  → `{"status":"ok"}`
- [ ] `curl localhost:8004/health`  → `{"status":"ok"}`
- [ ] Mesh auth wired correctly (should be **401**, proving the guard is on):
  ```bash
  curl -s -o /dev/null -w "%{http_code}\n" localhost:8002/match/anyuser              # -> 401
  curl -s -o /dev/null -w "%{http_code}\n" -X DELETE localhost:8003/users/anyuser    # -> 401
  curl -s -o /dev/null -w "%{http_code}\n" -X DELETE localhost:8004/users/anyuser    # -> 401
  ```

---

## 6. Operational gotchas (the "notes" — don't skip)
- [ ] **Matching needs the brain reachable.** Scoring pulls the living profile from the brain's
      `GET /users/{id}/profile`. When the nightly sleep pass runs and asks matching for a
      recommendation, the **brain API must be up** for matching to answer. If you run the sleep
      pass as a standalone process (`uv run alik-sleep`) on a box where the brain API isn't
      running, job matching produces nothing that night (everything else in the pass still works).
- [ ] **Same `SERVICE_TOKEN` everywhere**, or you'll see 401s between services (and the brain's
      profile read will reject matching). The brain's profile guard is only enforced **when the
      token is set** — so an empty token silently disables the guard (fine for local dev, **not**
      for production).
- [ ] **Ports must be distinct**: 8000/8001/8002/8003/8004, Postgres 5432 (brain) / 5433 (matching)
      / 5434 (connections) / 5435 (rendezvous), Redis 6379, FalkorDB 6380. The four Postgres
      instances are separate on purpose.
- [ ] **`delete()` is loud by design.** `DELETE /users/{id}` on the brain erases brain memory
      (incl. the Phase-8 `social_events`), then auth, then matching, then connections, then
      rendezvous. If any backend is unreachable it **raises** rather than reporting partial
      success — re-run once everything is back; the ops are idempotent. This is the
      right-to-erasure path; treat a failure as "not yet deleted." (A rendezvous meet involves
      two people; erasing one deletes the whole meet, so nothing about the erased user survives.)
- [ ] **Nightly sleep pass + hourly proactivity** run via APScheduler inside the brain process
      (optional dep). Confirm `apscheduler` is installed (`uv sync` with the `scheduler` extra) if
      you rely on automatic nightly profile/job passes; otherwise trigger `uv run alik-sleep`
      yourself on a cron.
- [ ] **Job catalog** lives at `services/matching/data/jobs.json` now (not in the brain). Add or
      edit jobs there; no code change needed. A malformed catalog fails the matching service
      loudly at startup.
- [ ] **Connections needs the brain reachable** — both ways. Its scoring/eval read the brain
      Profile API, and `surface_pass`/`cluster_pass` queue check-ins via the brain's
      `POST /users/{id}/checkins`. If the brain API is down, those passes produce nothing that
      cycle and retry next run (no state is written on a failed queue).
- [ ] **`pending_checkins.payload` (jsonb) auto-applies** via the brain's `schema.sql` on connect
      (idempotent `ALTER … ADD COLUMN IF NOT EXISTS`). Confirm the column exists before the first
      `people_match` check-in is queued — it carries `candidate_id`/`group_id` for the callback.
- [ ] **Connections has its own Anthropic key** (`ANTHROPIC_API_KEY`) used only by `eval_pass`.
      Size/scope its billing + rate limits alongside (or separate from) the brain's own usage.

---

## 7. Pre-flight (run the test suites before shipping)
- [ ] Brain: `uv run pytest && uv run ruff check . && uv run ruff format --check src tests`
- [ ] Auth: `cd services/auth && uv run pytest && uv run ruff check .`
- [ ] Matching: `cd services/matching && uv run pytest && uv run ruff check .`
- [ ] Connections: `cd services/connections && uv run pytest && uv run ruff check .`
  > The infra-touching brain tests skip automatically unless `ALIK_DATABASE_URL` /
  > `ALIK_REDIS_URL` (and `ALIK_FALKORDB_URL` for graph tests) are set — set them to run the
  > real-DB tests against docker-compose before going live.

---

## 8. Post-deploy verification (optional but recommended)
- [ ] Sign up a test user via auth (`POST /auth/signup`), have a short conversation via the
      brain (`/chat`), end the session (`/sessions/{id}/end`).
- [ ] Confirm the assembled profile reads back:
      `curl -H "X-Service-Token: <MESH_TOKEN>" localhost:8000/users/<id>/profile`
- [ ] Erase the test user end-to-end: `curl -X DELETE localhost:8000/users/<id>` → confirm the
      profile, auth account, matching, **and connections** data are all gone (do one real
      end-to-end deletion against staging — the fan-out is unit-tested with a fake client only).

---

## 9. Connections service (people-matching) — extra launch steps

The connections pipeline runs as **five staggered cron passes**, each consuming the previous
pass's output. Confirm the schedule leaves enough gap (defaults already do):

| Pass | Default cron | Console script | Reads |
|---|---|---|---|
| ingest | `0 */6 * * *` | `connections-ingest` | auth roster + brain Profile API |
| score | `0 2 * * *` | `connections-score` | ingested snapshots (kernel) |
| eval | `0 3 * * *` | `connections-eval` | candidate scores (LLM) |
| surface | `0 4 * * *` | `connections-surface` | eval results → brain check-ins (1:1) |
| cluster | `0 5 * * *` | `connections-cluster` | interest graph + scores → group check-ins |

- [ ] **Run the chain once manually against a small seed pool** before trusting the schedule:
      `uv run connections-ingest && uv run connections-score && uv run connections-eval &&
      uv run connections-surface && uv run connections-cluster`. Inspect the actual
      `candidate_scores`, `eval_results`, and `group_candidates` rows (and the `reason` strings)
      to sanity-check matching quality and tone before real users see anything.
- [ ] **Confirm `ANTHROPIC_API_KEY` billing/rate limits** are sized for `eval_pass` (it makes one
      call per directed candidate pair on the shortlist) — separate from the brain's usage.
- [ ] **Smoke-test the companion's `PEOPLE_MATCH` / `PEOPLE_MATCH_GROUP` openers in a real chat**,
      not just unit tests — the "warm friend, never an app/algorithm" tone is the whole point and
      is hard to unit-test convincingly. Queue a test check-in (`POST /users/{id}/checkins`), open
      a session, and read the actual opener + the yes/no callback.
- [ ] **Scheduler dependency:** the five crons run via APScheduler inside the connections process,
      but `apscheduler` is an **optional** dep (`main.py` degrades to no scheduler if absent). To
      use the in-process crons, `uv sync --extra scheduler`; otherwise trigger the console scripts
      from an external cron instead.

### Monitoring (BUILT — pass-run digest + alerting)
Each pass now records its `PASS_SUMMARY` to the `pass_runs` table (best-effort; a persistence
error never fails the pass). Read it three ways:
- [ ] **Daily digest** of the five passes (counts + failure rate per pass over a window): the
      `connections-digest` console script, a daily in-process scheduler job (`DIGEST_CRON`, default
      06:30), or `GET /digest`.
- [ ] **Alert** when the `eval` pass's LLM-failure rate is ≥ `EVAL_ERROR_RATE_THRESHOLD`
      (default 0.2) — usually an Anthropic rate-limit/outage, not a data problem — or when a pass
      hasn't run in the window (a cron that stopped firing). Alerts log at WARNING as
      `CONNECTIONS ALERT: …`; wire those to your pager/log alerting.
- [ ] Tunables: `DIGEST_WINDOW_HOURS` (24), `EVAL_ERROR_RATE_THRESHOLD` (0.2), `DIGEST_CRON`.

---

## 10. Rendezvous service (meeting coordination) — extra launch steps

The rendezvous service turns an accepted introduction into an actual meeting, then remembers
how it went. It's driven by one recurring pass plus companion-delivered check-ins:

- [ ] **The advance pass** (`rendezvous-advance` console script, or the in-process `ADVANCE_CRON`
      job — default every 30 min) walks each active meet and queues the next coordination
      check-in (pref → confirm → followup) via the brain. It's idempotent (per-stage asked-flags)
      and brain-outage safe (never marks "asked" unless the brain accepted the check-in). Use
      `uv sync --extra scheduler` for the in-process cron, or trigger the console script externally.
- [ ] **A meet is created automatically** when two people MUTUALLY accept a connections intro
      (connections POSTs `/meets`). Nothing to run — just confirm `RENDEZVOUS_URL` is set on the
      connections service and `ALIK_RENDEZVOUS_SERVICE_URL` on the brain.
- [ ] **Smoke-test the coordination openers in a real chat** — the "warm friend helping two
      people meet, never a scheduling app" tone is the point and is hard to unit-test. The other
      person stays anonymous ("someone who loves pottery"), and the MVP keeps *where/when* vague
      (a free-text relay — no parsing yet; an LLM plan-negotiation step is the planned 8.1
      fast-follow, see `docs/PHASE_8_RENDEZVOUS.md`).
- [ ] **Matchmaking memories** land back in the brain (`social_events`): "arranged to meet
      someone who loves pottery", "met …". They're per-user and anonymized, and erased by
      `delete()`.
