# alik

A voice-first AI companion that learns you by listening day to day, then (later)
introduces you to compatible people in real life. We're building the **brain first,
in text** — voice is the final phase and just an I/O swap.

**Current phase: 1 — companion loop + working/short-term memory (text).** A text
chat companion that feels continuous across sessions: tell it something today,
come back days later, and it recalls earlier conversations without being reminded.

See `CLAUDE.md` for the golden rules and the full phase map.

## Services

alik is split into independent services, each on its own port:

| Service | Dir | Port | Backed by | Purpose |
|---|---|---|---|---|
| Companion brain API | `src/alik/` | **8000** | Postgres + Redis + FalkorDB (docker-compose) | the text-in/text-out brain (memory, patterns, commitments, proactivity) |
| Auth + User Profile | `services/auth/` | **8001** | Supabase (auth + Postgres + storage) | email/password auth and the user profile (name, age, city, photo) |

`services/auth/` is a **standalone microservice** — its own `pyproject.toml`, venv, and
datastore. It does not import or share anything with the brain. See
[`services/auth/README.md`](services/auth/README.md) for its setup, schema SQL, and
endpoints. The brain on port 8000 is documented below.

## How it works

```
            you (CLI or HTTP)
                   │ text
                   ▼
            Companion (brain)──────────── text in, text out; no I/O, no driver imports
              │            │
   retrieve / write     stream / summarize
              ▼            ▼
            Memory       LLMClient
          ┌────┴────┐   (AnthropicLLM → claude-sonnet-4-6, configurable)
          ▼         ▼
   Redis hot     Postgres
   buffer        episodic
   (working      summaries
    memory)      (per session)
```

- **Working memory** (Redis): the live session's turns. Self-expires.
- **Episodic memory** (Postgres): one summary per ended session, with `user_id` + `created_at`.
- On each turn the companion injects recent episodic summaries into the system prompt.
- At session end it summarizes the conversation and persists it — that's what makes
  the *next* session feel continuous.

All memory access goes through the `Memory` interface. `Memory.delete(user_id)` fully
erases a user (Redis + Postgres) — a legal requirement, not a feature.

## Requirements

- Python 3.12+
- Docker (for Postgres 16 + Redis via `docker-compose.yml`)
- An Anthropic API key (for live model calls)
- Optional: [uv](https://docs.astral.sh/uv/) — the project is uv-native, but plain
  `pip` works too.

## Setup

```bash
# 1. Config
cp .env.example .env        # then edit ALIK_ANTHROPIC_API_KEY

# 2. Infrastructure
docker compose up -d        # Postgres 16 + Redis 7

# 3. Dependencies  (pick one)
uv sync                                     # with uv
# --- or without uv: ---
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e . --group dev
```

The episodic table is created automatically on startup (`init_db()` runs `schema.sql`,
idempotently) — no migration step in Phase 1.

## Talk to it

**Terminal CLI**

```bash
uv run alik          # or, in an activated venv: alik
```

Type messages; `/quit` (or Ctrl-D) ends the session, which triggers summarization
into episodic memory. `ALIK_USER_ID` controls whose memory you're using (default
`local-user`); each run is a new session.

**HTTP API**

```bash
uv run uvicorn alik.api:app --reload      # or: uvicorn alik.api:app --reload
```

| Method | Path | Body | Purpose |
|---|---|---|---|
| `POST` | `/chat` | `{user_id, session_id, message}` | streams the reply (text/plain) |
| `POST` | `/sessions/{session_id}/end` | `{user_id}` | summarize + persist episodic memory |
| `DELETE` | `/users/{user_id}` | — | hard-erase everything for the user |

```bash
curl -N -X POST localhost:8000/chat \
  -H 'content-type: application/json' \
  -d '{"user_id":"alice","session_id":"s1","message":"My dog is named Rufus."}'

curl -X POST localhost:8000/sessions/s1/end \
  -H 'content-type: application/json' -d '{"user_id":"alice"}'
```

## Configuration

All settings are read from the environment with the `ALIK_` prefix (see `.env.example`).
The companion's runtime model is **configurable and defaults to `claude-sonnet-4-6`** —
never hardcoded.

| Variable | Default | Notes |
|---|---|---|
| `ALIK_ANTHROPIC_API_KEY` | — | required for live calls |
| `ALIK_COMPANION_MODEL` | `claude-sonnet-4-6` | the model the *app* calls |
| `ALIK_COMPANION_MAX_TOKENS` | `1024` | |
| `ALIK_DATABASE_URL` | `postgresql://alik:alik@localhost:5432/alik` | |
| `ALIK_REDIS_URL` | `redis://localhost:6379/0` | |
| `ALIK_WORKING_BUFFER_TTL_SECONDS` | `21600` | working buffer self-expiry (~6h) |
| `ALIK_EPISODE_RETRIEVE_LIMIT` | `10` | recent summaries injected per turn |
| `ALIK_PERSONA_PATH` | packaged `persona.txt` | optional override |
| `ALIK_USER_ID` | `local-user` | used by the CLI |

## Tests

Two layers:

- **Infra-free** (`test_prompt.py`, `test_companion.py`) — pure logic and the
  cross-session continuity proof against an in-memory `Memory` double. Always run.
- **Integration** (`test_memory.py`, `test_continuity.py`, `test_api.py`) — the real
  Postgres + Redis path. They **skip** unless the DB/Redis URLs are set.

The LLM is always faked in tests (deterministic, no network).

```bash
# Infra-free only (no Docker needed)
uv run pytest tests/test_prompt.py tests/test_companion.py

# Everything, including the real DB path
docker compose up -d
export ALIK_DATABASE_URL=postgresql://alik:alik@localhost:5432/alik
export ALIK_REDIS_URL=redis://localhost:6379/0
uv run pytest
```

Lint / format: `uv run ruff check .` and `uv run ruff format .`.

## Project layout

```
src/alik/
  config.py          Settings (pydantic-settings)
  models.py          MemoryTier, MemoryRecord, RetrievedContext
  memory/
    base.py          Memory ABC (write / retrieve / invalidate / delete)
    pg_redis.py      Postgres + Redis impl — the only DB-driver importer
    schema.sql       episodic_memory DDL
  llm.py             LLMClient protocol + AnthropicLLM — the only anthropic importer
  prompt.py          pure prompt building + persona loader
  persona.txt        companion persona
  companion.py       the brain (text in, text out)
  api.py             FastAPI adapter
  cli.py             terminal adapter
tests/               prompt, companion, memory, continuity, api
```

## Not in this phase (seams left, not built)

Knowledge graph / FalkorDB, async extraction, nightly consolidation, pattern layer,
commitments/proactivity, voice. `MemoryTier` and `RetrievedContext` are extensible so
later tiers slot in without breaking callers.
