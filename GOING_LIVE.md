# Going live — operational checklist

Read this top to bottom when you're ready to deploy. alik is **three services** that talk to
each other over HTTP:

| Service | Path | Port | Datastore |
|---|---|---|---|
| Brain (companion + memory + living profile) | `src/alik/` | 8000 | Postgres 5432, Redis 6379, FalkorDB 6380 |
| Auth + profile (identity) | `services/auth/` | 8001 | Supabase (its own project) |
| Job matching | `services/matching/` | 8002 | Postgres 5433 |

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
  This same value goes in **all three** services (see step 2). Call it `<MESH_TOKEN>` below.

---

## 1. Infrastructure (Postgres × 2, Redis, FalkorDB)
- [ ] Start the datastores:
  ```bash
  docker compose up -d
  ```
- [ ] Confirm all four are healthy:
  ```bash
  docker compose ps        # postgres, redis, falkordb, matching-postgres all "healthy"
  ```
  > Schemas are created automatically on first connect (each service runs its own
  > `schema.sql`) — you do **not** run migrations by hand for the brain or matching.
  > (Supabase is the exception — see step 3.)

---

## 2. Secrets & environment

The mesh secret must be **identical** in all three services, or service-to-service calls
return 401:

| Service | Variable | Value |
|---|---|---|
| Brain | `ALIK_SERVICE_TOKEN` | `<MESH_TOKEN>` |
| Auth | `SERVICE_TOKEN` | `<MESH_TOKEN>` |
| Matching | `SERVICE_TOKEN` | `<MESH_TOKEN>` |

### Brain — `.env` (prefix `ALIK_`)
- [ ] `ALIK_ANTHROPIC_API_KEY` — **required** (the companion can't run without it).
- [ ] `ALIK_DATABASE_URL` — e.g. `postgresql://alik:alik@localhost:5432/alik`
- [ ] `ALIK_REDIS_URL` — e.g. `redis://localhost:6379/0`
- [ ] `ALIK_FALKORDB_URL` — e.g. `redis://localhost:6380/0`
- [ ] `ALIK_SERVICE_TOKEN` — `<MESH_TOKEN>`
- [ ] `ALIK_AUTH_SERVICE_URL` — e.g. `http://localhost:8001`
- [ ] `ALIK_MATCHING_SERVICE_URL` — e.g. `http://localhost:8002` (leave **empty to disable job
      matching** entirely — everything else still works)
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

---

## 3. Supabase setup (auth service only, one-time)
- [ ] In the Supabase dashboard: **Auth → Providers → Email → uncheck "Confirm email"**
      (so signup returns a live session immediately).
- [ ] Run the schema SQL from `services/auth/README.md` ("Schema SQL") in the SQL editor —
      creates the `profiles` table, RLS policies, and the `profile-photos` storage bucket.

---

## 4. Install each service
- [ ] `uv sync` (repo root — the brain)
- [ ] `cd services/auth && uv sync`
- [ ] `cd services/matching && uv sync`

---

## 5. Start the services
Bring up all three (order isn't strict — cross-service calls are made lazily and the brain
degrades gracefully if auth/matching are briefly unreachable):
- [ ] Brain: `uv run uvicorn alik.api:app --port 8000`
- [ ] Auth: `cd services/auth && uv run uvicorn auth_service.main:app --port 8001`
- [ ] Matching: `cd services/matching && uv run uvicorn matching_service.main:app --port 8002`

### Smoke test
- [ ] `curl localhost:8000/...`  (brain has no /health; hit `/chat` or check logs start clean)
- [ ] `curl localhost:8001/health`  → `{"status":"ok"}`
- [ ] `curl localhost:8002/health`  → `{"status":"ok"}`
- [ ] Mesh auth wired correctly (should be **401**, proving the guard is on):
  ```bash
  curl -s -o /dev/null -w "%{http_code}\n" localhost:8002/match/anyuser   # -> 401 (no token)
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
- [ ] **Ports must be distinct**: 8000/8001/8002, Postgres 5432 (brain) vs 5433 (matching),
      Redis 6379, FalkorDB 6380. The two Postgres instances are separate on purpose.
- [ ] **`delete()` is loud by design.** `DELETE /users/{id}` on the brain erases brain memory,
      then auth, then matching. If FalkorDB (or auth/matching) is unreachable it **raises** rather
      than reporting partial success — re-run once everything is back; the ops are idempotent.
      This is the right-to-erasure path; treat a failure as "not yet deleted."
- [ ] **Nightly sleep pass + hourly proactivity** run via APScheduler inside the brain process
      (optional dep). Confirm `apscheduler` is installed (`uv sync` with the `scheduler` extra) if
      you rely on automatic nightly profile/job passes; otherwise trigger `uv run alik-sleep`
      yourself on a cron.
- [ ] **Job catalog** lives at `services/matching/data/jobs.json` now (not in the brain). Add or
      edit jobs there; no code change needed. A malformed catalog fails the matching service
      loudly at startup.

---

## 7. Pre-flight (run the test suites before shipping)
- [ ] Brain: `uv run pytest && uv run ruff check . && uv run ruff format --check src tests`
- [ ] Auth: `cd services/auth && uv run pytest && uv run ruff check .`
- [ ] Matching: `cd services/matching && uv run pytest && uv run ruff check .`
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
      profile, auth account, and matching data are all gone.
