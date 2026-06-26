# alik — Auth + User Profile service

A **standalone** microservice for sign-up, login, and the user profile (name, age, city,
photo). It is completely separate from the companion brain in `src/alik/`: its own
dependencies, its own venv, its own datastore (Supabase), and its own port (**8001** —
the brain API runs on 8000).

It is backed entirely by **Supabase**:

- **Auth** — email/password (JWT sessions). We never roll our own auth or decode JWTs
  ourselves; token validation is delegated to `supabase.auth.get_user()`.
- **Postgres** — one `profiles` table.
- **Storage** — a public `profile-photos` bucket, one `{user_id}.jpg` per user.

## Architecture

```
              HTTP (port 8001)
                    │
              FastAPI routes/        (auth.py, profile.py)
                    │
              services/              (auth_svc.py, profile_svc.py)  ← business logic
                    │
              supabase_client.py     ← the ONLY module that imports the supabase SDK
                    │
        ┌───────────┴───────────┐
   anon-key client         service-key client
   (signup/login/refresh,  (profile insert, hard-delete user,
    token validation)       storage write/remove — bypasses RLS)
```

- **`supabase_client.py` is the single seam** to Supabase. Nothing else imports `supabase`.
- **`/auth/account` is a true hard erase** (photo → profile row → auth user), loud on
  failure — the same right-to-erasure principle as `Memory.delete()` in the brain.

## Supabase setup (one time)

1. Create a Supabase project; copy the URL + anon + service-role keys into `.env`
   (`cp .env.example .env`).
2. **Disable email confirmation** so signup returns a live session immediately:
   Dashboard → **Auth → Providers → Email → uncheck "Confirm email"**.
3. Run the schema SQL below in the Dashboard **SQL Editor**.
4. Create the storage bucket (the SQL below does this too).

### Schema SQL

```sql
-- profiles table
create table public.profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  name        text not null,
  age         int  not null check (age >= 25),
  city        text not null,
  photo_url   text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- keep updated_at fresh
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end; $$;

create trigger profiles_set_updated_at
  before update on public.profiles
  for each row execute function public.set_updated_at();

-- Row Level Security: a user can only see/modify their own row.
-- (The service-key client bypasses RLS for signup-insert and erasure.)
alter table public.profiles enable row level security;

create policy "own profile select" on public.profiles
  for select using (auth.uid() = id);
create policy "own profile update" on public.profiles
  for update using (auth.uid() = id);
create policy "own profile insert" on public.profiles
  for insert with check (auth.uid() = id);

-- storage bucket: public read, authenticated write
insert into storage.buckets (id, name, public)
values ('profile-photos', 'profile-photos', true)
on conflict (id) do nothing;

create policy "public read photos" on storage.objects
  for select using (bucket_id = 'profile-photos');
create policy "auth write own photo" on storage.objects
  for insert with check (bucket_id = 'profile-photos' and auth.role() = 'authenticated');
create policy "auth update own photo" on storage.objects
  for update using (bucket_id = 'profile-photos' and auth.role() = 'authenticated');
```

## Run

```bash
uv sync
uv run uvicorn auth_service.main:app --reload --port 8001
# or: uv run python -m auth_service.main   (reads PORT from env, default 8001)
curl localhost:8001/health        # -> {"status":"ok"}
```

## Endpoints

| Method | Path | Auth | Body | Returns |
|---|---|---|---|---|
| `GET` | `/health` | — | — | `{ status: "ok" }` |
| `POST` | `/auth/signup` | — | `{email, password, name, age, city}` | `{access_token, refresh_token, user_id}` (201). `age<25` → **403** "alik is for people 25 and older" |
| `POST` | `/auth/login` | — | `{email, password}` | `{access_token, refresh_token, user_id}` |
| `POST` | `/auth/logout` | Bearer | — | 204 |
| `POST` | `/auth/refresh` | — | `{refresh_token}` | `{access_token, refresh_token, user_id}` |
| `GET` | `/profile/me` | Bearer | — | full profile incl. `photo_url` |
| `PATCH` | `/profile/me` | Bearer | `{name?, city?}` | updated profile (age/email not editable) |
| `POST` | `/profile/me/photo` | Bearer | multipart `photo` (jpeg/png ≤5MB) | `{photo_url}` |
| `DELETE` | `/auth/account` | Bearer | — | 204 — **hard erase** (photo + profile + auth user) |

## Configuration

Read from the environment (the three Supabase keys use the `SUPABASE_` prefix):

| Variable | Notes |
|---|---|
| `SUPABASE_URL` | project URL |
| `SUPABASE_ANON_KEY` | anon/public key (user-context auth ops) |
| `SUPABASE_SERVICE_KEY` | service-role key (admin ops — keep secret) |
| `PORT` | default `8001` |

## Tests

Four required tests, **Supabase fully mocked** (no network): signup happy path,
signup age<25 → 403, login happy path, profile shape, photo bad-type rejection.

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
