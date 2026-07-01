-- rendezvous service schema (its own Postgres — never the brain's).
-- One row per meet being coordinated. Both participants inline (a/b); each side is only ever
-- told about the other ANONYMOUSLY (desc_a/desc_b), never a name. Erasing a participant deletes
-- the whole meet (WHERE user_a=$1 OR user_b=$1), so nothing about the erased user survives.
CREATE TABLE IF NOT EXISTS meets (
    id               text        PRIMARY KEY,        -- uuid4().hex
    user_a           text        NOT NULL,
    user_b           text        NOT NULL,
    desc_a           text        NOT NULL,           -- anonymized descriptor of B (shown to A)
    desc_b           text        NOT NULL,           -- anonymized descriptor of A (shown to B)
    status           text        NOT NULL,           -- coordinating|confirming|confirmed|followed_up|cancelled
    pref_a           text,
    pref_b           text,
    pref_asked_a     boolean     NOT NULL DEFAULT false,
    pref_asked_b     boolean     NOT NULL DEFAULT false,
    plan             text,
    confirm_a        boolean,
    confirm_b        boolean,
    confirm_asked_a  boolean     NOT NULL DEFAULT false,
    confirm_asked_b  boolean     NOT NULL DEFAULT false,
    followup_a       boolean,
    followup_b       boolean,
    followup_asked_a boolean     NOT NULL DEFAULT false,
    followup_asked_b boolean     NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_meets_status ON meets (status);
CREATE INDEX IF NOT EXISTS idx_meets_user_a ON meets (user_a);
CREATE INDEX IF NOT EXISTS idx_meets_user_b ON meets (user_b);
