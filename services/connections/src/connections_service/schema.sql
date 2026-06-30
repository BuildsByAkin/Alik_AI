-- People-matching ("connections") service — own datastore (all match state lives here).
-- Part 2: ingested user snapshots + the people<->interest graph. Candidate/eval/intro-state
-- tables arrive in Parts 3-5; group_candidates in Part 6 (it joins user_interests below).

-- Ingested user snapshot. age is stored for the record only — it is NOT a matching signal
-- and is intentionally NOT indexed for matching (25+ is gated at signup; auth's job).
CREATE TABLE IF NOT EXISTS users_pool (
    user_id          text PRIMARY KEY,
    state            text        NOT NULL,
    age              int,
    city             text,
    pool_ready       boolean     NOT NULL DEFAULT false,
    last_ingested_at timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pool_state_ready ON users_pool (state, pool_ready);

-- The interest taxonomy (seeded at startup from interests.py — single source of truth).
CREATE TABLE IF NOT EXISTS interest_nodes (
    id                text PRIMARY KEY,          -- "{broad_category}:{specific_interest}"
    broad_category    text NOT NULL,
    specific_interest text NOT NULL,
    canonical_label   text NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interest_broad ON interest_nodes (broad_category);

-- The people<->interest edges (the bipartite graph). Replace-set per user on each ingest.
CREATE TABLE IF NOT EXISTS user_interests (
    user_id          text NOT NULL,
    interest_node_id text NOT NULL REFERENCES interest_nodes(id),
    weight           real NOT NULL,
    source_fact_key  text NOT NULL,
    PRIMARY KEY (user_id, interest_node_id)
);
-- idx_ui_node drives both the Part-3 shared-interest join and the Part-6 "who shares node X".
CREATE INDEX IF NOT EXISTS idx_ui_node ON user_interests (interest_node_id);
CREATE INDEX IF NOT EXISTS idx_ui_user ON user_interests (user_id);

-- Per-user behavioral-dimension snapshot (the non-sensitive structured layer).
CREATE TABLE IF NOT EXISTS profile_dimensions (
    user_id    text NOT NULL,
    dimension  text NOT NULL,
    value      text NOT NULL,
    confidence real NOT NULL,
    status     text NOT NULL,
    PRIMARY KEY (user_id, dimension)
);

-- Part 3: the compatibility kernel's output, one directed row per (subject A, candidate B).
-- Scores are REPLACED on each scoring run (upsert by PK). explanation is structured jsonb.
CREATE TABLE IF NOT EXISTS candidate_scores (
    user_id_a         text        NOT NULL,
    user_id_b         text        NOT NULL,
    score             real        NOT NULL,
    interest_score    real        NOT NULL,
    dimension_score   real        NOT NULL,
    values_score      real        NOT NULL,
    confidence        real        NOT NULL,
    human_review_flag boolean     NOT NULL DEFAULT false,
    explanation       jsonb       NOT NULL,
    scored_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id_a, user_id_b)
);
CREATE INDEX IF NOT EXISTS idx_cand_a_score ON candidate_scores (user_id_a, score DESC);

-- Part 4: the LLM cross-evaluation's verdict per directed pair. final_confidence combines the
-- kernel and LLM confidences (computed on save). Upsert — refreshed on each eval run.
CREATE TABLE IF NOT EXISTS eval_results (
    user_id_a        text        NOT NULL,
    user_id_b        text        NOT NULL,
    would_click      boolean     NOT NULL,
    llm_confidence   real        NOT NULL,
    final_confidence real        NOT NULL,
    reason           text        NOT NULL,
    flag_for_review  boolean     NOT NULL DEFAULT false,
    flag_reason      text,
    eval_model       text        NOT NULL,
    evaled_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id_a, user_id_b)
);
CREATE INDEX IF NOT EXISTS idx_eval_surface ON eval_results (user_id_a, final_confidence DESC)
    WHERE would_click = true;

-- Part 5: every surfaced pair (the subject's view). status: shown (queued in the brain) ->
-- accepted | skipped (user responded via the companion). Drives shown-exclusion everywhere.
CREATE TABLE IF NOT EXISTS match_state (
    user_id      text        NOT NULL,
    candidate_id text        NOT NULL,
    status       text        NOT NULL,
    checkin_id   text,                       -- the brain PendingCheckin id
    surfaced_at  timestamptz,
    responded_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_ms_user_status ON match_state (user_id, status);

-- Part 6: a clustered group of mutually-compatible people who share a specific activity.
-- member_ids is sorted; the unique index dedups a re-found group (upsert keeps id + status).
CREATE TABLE IF NOT EXISTS group_candidates (
    group_id         text        PRIMARY KEY,   -- uuid4().hex
    interest_node_id text        NOT NULL,
    member_ids       text[]      NOT NULL,
    mean_score       real        NOT NULL,
    status           text        NOT NULL,       -- proposed | surfacing | surfaced | declined
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_gc_members ON group_candidates (interest_node_id, member_ids);
CREATE INDEX IF NOT EXISTS idx_gc_status ON group_candidates (status);
CREATE INDEX IF NOT EXISTS idx_gc_members_gin ON group_candidates USING gin (member_ids);
