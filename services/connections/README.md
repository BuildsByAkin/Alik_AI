# alik — Connections (people-matching) service

A **standalone** microservice that compares users' profiles to broker real-life
introductions — friendship and connection of every kind, **not** a dating app. Separate from
the companion brain (`src/alik/`): its own dependencies, venv, Postgres, and port (**8003** —
brain 8000, auth 8001, matching 8002).

It is a **pure consumer of the living profile**: the only way it reads about a user is the
brain's Profile API (`GET /users/{id}/profile`, `X-Service-Token`). It **never** touches the
brain's databases — all match state lives in this service's own Postgres.

> **Part 1 (this commit) is a scaffold only**: a runnable, deployable, empty shell — health +
> the deletion seam, no match logic yet. See the roadmap below.

## Roadmap
1. **Scaffold** ← you are here
2. Ingestion + interest graph (specific→broad hierarchy; people↔interest first-class)
3. Deterministic compatibility kernel (per-axis mixed similarity/complementarity + broadening)
4. LLM cross-evaluation + vetting confidence (cheap model; human-review flag seam)
5. Surfacing via companion `PendingCheckin` + match-state logging + brain delete fan-out wiring
6. Group-awareness (clustering over the people↔interest graph)

## Architecture (Part 1)

```
            HTTP (port 8003, X-Service-Token on every route except /health)
                  │
              routes.py ──> store.py (Postgres: match state)   [empty in Part 1]
                  │
            brain_client.py ──> brain Profile API (the only user read)
```

- **`create_app(*, store=…, brain_client=…)`** is injectable, so the API + logic are testable
  with `InMemoryStore` + a fake brain client — zero infra.
- `deps.verify_service_token` gates every route (fails closed if no token configured).

## Endpoints (Part 1)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | — | `{ "status": "ok" }` |
| `DELETE` | `/users/{user_id}` | `X-Service-Token` | erase this service's data for the user (no-op now; the seam exists from day one) |

## Run

```bash
uv sync
# bring up this service's Postgres (root docker-compose: connections-postgres on 5434)
uv run uvicorn connections_service.main:app --reload --port 8003
# or: uv run python -m connections_service.main
curl localhost:8003/health        # -> {"status":"ok"}
```

## Configuration

| Variable | Notes |
|---|---|
| `DATABASE_URL` | this service's Postgres (default `…:5434/connections`) |
| `BRAIN_URL` | companion brain base URL (Profile API source) |
| `SERVICE_TOKEN` | shared mesh secret — must equal the brain's `ALIK_SERVICE_TOKEN` |
| `PORT` | default `8003` |
| `LLM_MODEL` | cross-eval model (Part 4); cheap/Haiku-class, never hardcoded |
| `AGE_FILTER_MODE` | `off` \| `soft` \| `hard` (Part 3); 25+ already gated at signup |

## Tests

```bash
uv run pytest          # health + deletion seam, InMemoryStore + fake brain (no infra)
uv run ruff check . && uv run ruff format --check .
```
