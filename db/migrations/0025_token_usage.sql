-- Spend/rate-cap foundation (PLAN.md phase 4, PR 1). CLAUDE.md §9: "cap
-- token spend per conversation and per number per day." services/agent/llm/
-- conversation.py's own docstring says the webhook must not merge until
-- this enforcement exists.
--
-- One new append-only table, no columns added to conversations: a
-- per-conversation counter column would duplicate state the per-number-
-- per-day cap needs a log table for anyway, so both caps derive from the
-- same source of truth instead of two representations of the same fact —
-- same "no derived/redundant state" habit as conversations having no
-- `escalated` column (migration 0024).
--
-- customer_phone is duplicated from conversations rather than joined for
-- every export/erasure query — ARCHITECTURE.md §10's stated reason: any
-- future table carrying customer data must carry the same column, same
-- name. Right to erasure is satisfied via conversation_id's ON DELETE
-- CASCADE: conversations_erase_customer (migration 0024) deletes the
-- conversation row, which cascades here exactly as it already does to
-- messages/escalations — no separate erasure function needed for this
-- table.
--
-- RLS: ENABLE + FORCE + REVOKE ALL from anon/authenticated + GRANT to
-- service_role, with zero CREATE POLICY statements — matches
-- conversations/messages/escalations/bookings (migration 0024) exactly.
-- CLAUDE.md rule 11's FOR-SELECT-paired-with-write requirement applies to
-- tables *with* policies (the admin-dashboard tables from phase 3); this
-- table has none, so it doesn't apply — same reasoning 0024 already used.
-- This trips the ACL drift gate (PR #29's CI job) by touching
-- db/migrations/**, as expected.
CREATE TABLE token_usage (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id bigint NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    customer_phone text NOT NULL,
    prompt_tokens integer NOT NULL
    CONSTRAINT token_usage_prompt_tokens_non_negative CHECK (prompt_tokens >= 0),
    candidates_tokens integer NOT NULL
    CONSTRAINT token_usage_candidates_tokens_non_negative
    CHECK (candidates_tokens >= 0),
    total_tokens integer NOT NULL
    CONSTRAINT token_usage_total_tokens_non_negative CHECK (total_tokens >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- conversation_id serves check_token_spend_caps' per-conversation SUM;
-- created_at serves its global daily-cap SUM's range scan. No
-- customer_phone-leading index: the per-number-per-day rate cap queries
-- messages, not this table, and the daily-cap query has no
-- customer_phone predicate for a composite index to serve — add one only
-- once a real query needs it (e.g. a future per-customer spend export).
CREATE INDEX token_usage_conversation_id_idx ON token_usage (conversation_id);
CREATE INDEX token_usage_created_at_idx ON token_usage (created_at);

ALTER TABLE token_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE token_usage FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE token_usage FROM anon, authenticated;
-- Append-only, same shape as quotes (migration 0008/0013): a usage log
-- must never be mutated after the fact, so UPDATE/DELETE are revoked
-- explicitly rather than just not granted — Supabase's own default ACL
-- grants ALL privileges to service_role on every new table regardless of
-- what is explicitly GRANTed (migration 0013's comment, confirmed
-- empirically), so an un-revoked table would be silently mutable.
GRANT SELECT, INSERT ON TABLE token_usage TO service_role;
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE token_usage
FROM service_role;
