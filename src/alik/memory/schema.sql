-- Short-term episodic memory: one row per ended session.
CREATE TABLE IF NOT EXISTS episodic_memory (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     text        NOT NULL,
    session_id  text        NOT NULL,
    summary     text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Phase 3 (sleep pass): salient episodes are promoted (never decayed); stale ones
-- are soft-deleted via decayed_at (kept for audit, excluded from retrieve()).
ALTER TABLE episodic_memory ADD COLUMN IF NOT EXISTS promoted boolean NOT NULL DEFAULT false;
ALTER TABLE episodic_memory ADD COLUMN IF NOT EXISTS decayed_at timestamptz;  -- null = live

-- Drives "recent episodic context for a user".
CREATE INDEX IF NOT EXISTS idx_episodic_user_created
    ON episodic_memory (user_id, created_at DESC);

-- Phase 3: one human-readable reflection per user per day (history kept for audit).
CREATE TABLE IF NOT EXISTS reflections (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      text        NOT NULL,
    content      text        NOT NULL,
    generated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reflections_user_generated
    ON reflections (user_id, generated_at DESC);

-- Phase 5 (proactivity): queued proactive openers. The companion delivers the most
-- recent undelivered row at the next session open, then sets delivered_at. At most
-- one undelivered row per user is ever queued (enforced in proactivity.decide_for_user).
CREATE TABLE IF NOT EXISTS pending_checkins (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       text        NOT NULL,
    commitment_id text,                     -- null = non-commitment check-in
    checkin_type  text        NOT NULL,     -- due/upcoming_commitment | general_checkin | job_* | people_match
    message_hint  text        NOT NULL,     -- what the companion should open with
    payload       jsonb,                    -- structured data for richer check-ins (Part 5 people_match)
    created_at    timestamptz NOT NULL DEFAULT now(),
    delivered_at  timestamptz               -- null = not yet delivered
);
-- Part 5: add the payload column to a pre-existing table (idempotent).
ALTER TABLE pending_checkins ADD COLUMN IF NOT EXISTS payload jsonb;

-- Drives "get the undelivered check-in for a user".
CREATE INDEX IF NOT EXISTS idx_checkins_user_delivered
    ON pending_checkins (user_id, delivered_at);

-- Phase 5.2 (reflect-back cadence): per-user cooldown so the companion doesn't ask a
-- reflect-back question every single session. ``remaining`` is the number of upcoming
-- sessions to skip; set to N when a reflect-back fires, decremented at each session end.
CREATE TABLE IF NOT EXISTS reflect_back_cooldown (
    user_id   text PRIMARY KEY,
    remaining int  NOT NULL DEFAULT 0
);

-- Living profile (behavioral layer): one row per (user, taxonomy axis). The dominant
-- value + accumulating confidence + observation_count paint a stable picture over time;
-- status separates CONFIRMED dimensions from ones still needing a gentle check.
-- provenance (episode/signal ids) is mandatory, mirroring InferredTraits.
CREATE TABLE IF NOT EXISTS profile_dimensions (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             text        NOT NULL,
    dimension           text        NOT NULL,   -- taxonomy axis (alik.profile.TAXONOMY)
    value               text        NOT NULL,   -- taxonomy value for that axis
    content             text        NOT NULL,   -- human sentence (drives the soft-confirm question)
    confidence          real        NOT NULL DEFAULT 0,
    observation_count   int         NOT NULL DEFAULT 1,
    status              text        NOT NULL DEFAULT 'unconfirmed',  -- unconfirmed|confirmed|corrected
    surfaced_in_session text,                   -- last session we soft-confirmed it in
    source_session_id   text,                   -- session a confirm/correct was stated in
    provenance          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    valid_from          timestamptz NOT NULL DEFAULT now(),  -- first observed
    last_observed_at    timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, dimension)
);

CREATE INDEX IF NOT EXISTS idx_profile_dim_user ON profile_dimensions (user_id);

-- Job matching (recommendation log + engagement flag) moved OUT of the brain into the
-- standalone matching microservice (services/matching), which owns its own Postgres.

-- Phase 5 (commitment lifecycle) lives in FalkorDB (schemaless), documented here:
--   Commitment node props: status (pending|due|resolved_kept|resolved_dropped),
--   expected_by timestamptz|null, resolved_at timestamptz|null,
--   follow_through bool|null, reminded_count int, last_reminded_at timestamptz|null.
-- Pre-Phase-5 commitment nodes have no status property and are read as 'pending'.
