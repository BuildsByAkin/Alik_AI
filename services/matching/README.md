# alik — Job-matching service

A **standalone** microservice that surfaces relevant paid work for a user. It is separate
from the companion brain (`src/alik/`): its own dependencies, its own venv, its own Postgres,
and its own port (**8002** — brain 8000, auth 8001).

It is a **pure consumer of the living profile**: given a `user_id`, it reads the assembled
profile (`facts` + `confirmed_traits`) from the brain's Profile API, scores a hand-curated
catalog (`data/jobs.json`), and tracks the recommendation lifecycle in its own datastore. It
never talks to the user — the brain delivers any recommendation through the companion and posts
the outcome back here.

## Architecture

```
                HTTP (port 8002, X-Service-Token on every route)
                      │
                routes.py
                      │
        ┌─────────────┼───────────────┐
   selection.py   store.py        brain_client.py
   (cooldown/     (Postgres:       (reads the brain's
    one-thread)    rec log +        Profile API for
                   job_active)      facts + traits)
                      │
                  scorer.py  ← deterministic, catalog.py is the source of truth
```

- **One open thread per user**: while a recommendation is unresolved (or in a post-outcome
  cooldown) no new one is picked. Never the same job twice; never a disliked partner again.
- **No LLM here** — scoring is deterministic; the brain classifies the follow-up reply into an
  outcome and POSTs the structured value.

## Endpoints (all require `X-Service-Token`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | liveness (no auth) |
| `GET` | `/match/{user_id}` | pick + log the next recommendation (reads Profile API), or `null` |
| `GET` | `/users/{user_id}/followup-due` | a delivered rec past its 3-day window, or `null` |
| `GET` | `/users/{user_id}/open-recommendation` | the open, undelivered rec (delivery setup) |
| `GET` | `/users/{user_id}/pending-followup` | the rec awaiting an outcome |
| `POST` | `/recommendations/{id}/delivered` | stamp delivered_at |
| `POST` | `/recommendations/{id}/followup-sent` | stamp follow_up_sent_at |
| `POST` | `/recommendations/{id}/outcome` | `{user_id, outcome}` — record + flip job_active if liked/loved |
| `GET` | `/users/{user_id}/job-active` | `{active: bool}` |
| `DELETE` | `/users/{user_id}` | erase this service's data (cross-service deletion) |

## Run

```bash
uv sync
# bring up this service's Postgres (see root docker-compose: matching-postgres on 5433)
uv run uvicorn matching_service.main:app --reload --port 8002
# or: uv run python -m matching_service.main
curl localhost:8002/health        # -> {"status":"ok"}
```

## Configuration

| Variable | Notes |
|---|---|
| `DATABASE_URL` | this service's Postgres (default `…:5433/matching`) |
| `BRAIN_URL` | companion brain base URL (Profile API source) |
| `SERVICE_TOKEN` | shared mesh secret — must equal the brain's `ALIK_SERVICE_TOKEN` |
| `PORT` | default `8002` |
| `CATALOG_PATH` | default `data/jobs.json` |

## Tests

```bash
uv run pytest          # scorer + API, in-memory store + fake brain (no infra)
uv run ruff check . && uv run ruff format --check .
```
