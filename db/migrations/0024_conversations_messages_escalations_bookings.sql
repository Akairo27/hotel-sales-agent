-- Agent-layer tables ARCHITECTURE.md §10 explicitly says do not exist yet:
-- conversations, messages, escalations, bookings. §10 also says the shape
-- of these four must be settled before the first migration that creates
-- them, not after, because of three capabilities that get painful to retrofit
-- once customer data is scattered across tables with no common key:
--
--   1. Right to erasure — every table here must carry the exact same
--      customer_phone column (name and type), the same key quotes.customer_phone
--      already uses (migration 0008), so a future export/delete request is a
--      handful of `WHERE customer_phone = :phone` queries, not a manual
--      per-table audit at request time.
--   2. Right to access/export — same requirement, same column.
--   3. Retention policy — the actual number (how long a conversation is kept)
--      is the client's decision, not a technical default, and is deliberately
--      NOT encoded here. last_message_at exists so a future worker job (same
--      pattern as the existing hold-expiry worker, §6) can act on whatever
--      number gets decided, without needing a schema change to do it.
--
-- conversations/messages/escalations use real DELETE for erasure (unlike
-- quotes' NULL-the-phone-only approach): there is no audited financial record
-- to preserve here, just conversation content, so removing the row entirely
-- is both simpler and a stronger guarantee than redacting one column.
-- bookings is the opposite case — an accounting record analogous to quotes —
-- so it follows quotes' redact-in-place pattern instead; see its own section
-- below.
--
-- RLS: all four follow holds/quotes exactly (ENABLE + FORCE, REVOKE ALL from
-- anon/authenticated, GRANT to service_role) with zero CREATE POLICY
-- statements. service_role has BYPASSRLS (tests/conftest.py's _ROLES_SQL
-- mirrors the real Supabase role), so RLS-with-no-policies plus the REVOKE is
-- what actually blocks anon/authenticated, same reasoning as every table
-- these two reference. None of the four have an admin-dashboard-facing role
-- yet — that needs its own admin_access migration later, same shape as
-- migration 0014, if a staff-facing escalations screen gets built.

CREATE TABLE conversations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_phone text NOT NULL,
    -- The quote currently being negotiated, if any. A conversation can
    -- produce more than one quote (different dates asked about in the same
    -- thread); this is deliberately a pointer to exactly one, not derived
    -- from "the newest quotes row for this conversation", because
    -- ARCHITECTURE.md §7 describes negotiation state as something the
    -- conversation row itself holds, not something re-derived from history.
    active_quote_id bigint REFERENCES quotes (id),
    -- ARCHITECTURE.md §7: "three concessions maximum" is a negotiation rule
    -- enforced by services/agent's (not yet built) negotiation logic as a
    -- named constant, the same way CLAUDE.md asks for MAX_CONCESSIONS = 3
    -- in code rather than a magic number — not duplicated here as a CHECK,
    -- because unlike inventory overselling (CLAUDE.md rule 3) a wrong
    -- concession count is not a financial-loss failure mode a DB constraint
    -- needs to be the last line of defense against.
    concession_count smallint NOT NULL DEFAULT 0
    CONSTRAINT conversations_concession_count_non_negative
    CHECK (concession_count >= 0),
    -- ARCHITECTURE.md §9 (CLAUDE.md's LLM integration rules): conversation
    -- turns are capped. The cap itself is agent-layer logic, not encoded
    -- here, same reasoning as concession_count above.
    turn_count integer NOT NULL DEFAULT 0
    CONSTRAINT conversations_turn_count_non_negative CHECK (turn_count >= 0),
    -- No escalated/is_escalated column: escalation state is derived from
    -- whether an open row exists in escalations (resolved_at IS NULL), the
    -- same reasoning holds.sql's own header comment gives for having no
    -- status column at all — a derived value can never drift from the rows
    -- that actually define it.
    last_message_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE conversations FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE conversations TO service_role;
-- No TRUNCATE/REFERENCES/TRIGGER: Supabase's own default ACL grants these to
-- service_role on every new table regardless of the GRANT line above
-- (confirmed empirically for quotes/audit_log — see migration 0013's
-- comment); no service_role code path here has a legitimate reason to issue
-- a schema-bypassing TRUNCATE, so it is revoked explicitly rather than left
-- to the default. Unlike quotes/audit_log, DELETE itself stays granted:
-- deleting a conversation is the actual erasure and retention mechanism for
-- this table, not something to route through a narrower function.
REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLE conversations FROM service_role;

CREATE TABLE messages (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id bigint NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    -- Duplicated from conversations.customer_phone rather than joined for
    -- every export/erasure query — ARCHITECTURE.md §10's stated reason: "any
    -- future table (bookings, conversations, messages, escalations) must
    -- carry the same column, same name" — not for referential correctness,
    -- which conversation_id's FK already provides.
    customer_phone text NOT NULL,
    direction text NOT NULL
    CONSTRAINT messages_direction_valid CHECK (direction IN ('inbound', 'outbound')),
    -- Only inbound (customer-sent) messages carry a real WhatsApp message
    -- id; NULL for outbound messages the agent itself generates. Backs
    -- CLAUDE.md's idempotency requirement ("the same WhatsApp message_id
    -- processed twice must produce one booking") for the message-logging
    -- path specifically — create_hold's own idempotency_key (migrations
    -- 0004-0005) is the separate, already-built mechanism for the
    -- inventory-mutating path.
    whatsapp_message_id text,
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX messages_whatsapp_message_id_unique ON messages (whatsapp_message_id)
WHERE whatsapp_message_id IS NOT NULL;

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE messages FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE messages TO service_role;
REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLE messages FROM service_role;

CREATE TABLE escalations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id bigint NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    customer_phone text NOT NULL,
    -- Free text, not a closed CHECK list: ARCHITECTURE.md §6-§7 name two
    -- concrete triggers today (cash-on-arrival confirmation, an output-guard
    -- violation), but the agent's full tool surface — and therefore the
    -- full set of escalation reasons — does not exist yet. A closed list
    -- invented ahead of the code that would actually raise each reason
    -- would be a guess, not a constraint; CLAUDE.md rule "never invent"
    -- applies here.
    reason text NOT NULL
    CONSTRAINT escalations_reason_not_blank CHECK (length(btrim(reason)) > 0),
    opened_at timestamptz NOT NULL DEFAULT now(),
    responded_at timestamptz
    CONSTRAINT escalations_responded_after_opened
    CHECK (responded_at IS NULL OR responded_at >= opened_at),
    resolved_at timestamptz
    CONSTRAINT escalations_resolved_after_opened
    CHECK (resolved_at IS NULL OR resolved_at >= opened_at),
    assigned_to uuid REFERENCES app_users (id),
    notes text
);

ALTER TABLE escalations ENABLE ROW LEVEL SECURITY;
ALTER TABLE escalations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE escalations FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE escalations TO service_role;
REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLE escalations FROM service_role;

-- Activates the FK migration 0008 anticipated but could not create: "the
-- conversations table this will eventually reference doesn't exist before
-- phase 4." It exists now.
--
-- ON DELETE SET NULL, not the default RESTRICT: quotes is append-only and
-- never deleted (migration 0013) regardless of what happens to the
-- conversation that produced it, so conversations_erase_customer's plain
-- DELETE FROM conversations must not be blocked by a quote still pointing
-- at the row being erased — it needs to survive, unlinked, the same
-- pricing record intact, exactly like quotes_erase_customer_phone already
-- leaves everything but the phone untouched.
ALTER TABLE quotes
ADD CONSTRAINT quotes_conversation_id_fkey
FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE SET NULL;

CREATE TABLE bookings (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- One booking per hold: a hold is confirmed into exactly one booking
    -- (services/inventory's confirm_hold, ARCHITECTURE.md §6), never split
    -- or reused.
    -- ARCHITECTURE.md §10 flags holds (migrations 0004-0005) as still
    -- missing any customer-linking column of its own, and is explicit that
    -- this must not be "left to bookings alone" to close — that gap is
    -- still open here. bookings.customer_phone below covers a booking
    -- once confirmed; a hold still cannot be tied to a customer before
    -- that point except through hold_id -> bookings once one exists. The
    -- fix belongs to whichever PR wires create_hold from the agent layer
    -- (§10's own words), not this one.
    hold_id bigint NOT NULL UNIQUE REFERENCES holds (id),
    quote_id bigint NOT NULL REFERENCES quotes (id),
    -- Nullable, not NOT NULL, despite every real booking having all three at
    -- creation time: bookings_erase_customer below sets these to NULL on a
    -- right-to-erasure request without deleting the row (the accounting
    -- record must survive), the same reasoning and the same nullable-despite
    -- always-populated-in-practice shape as quotes.customer_phone.
    customer_phone text,
    full_name text,
    nationality text,
    status text NOT NULL DEFAULT 'confirmed'
    CONSTRAINT bookings_status_valid CHECK (status IN ('confirmed', 'cancelled')),
    payment_status text NOT NULL
    CONSTRAINT bookings_payment_status_valid
    CHECK (payment_status IN ('pending', 'paid_partial', 'paid_full', 'refunded')),
    -- Matches the worker's "accounting sync" job named in ARCHITECTURE.md §2
    -- and §9's integration table — a booking starts pending and the worker
    -- flips it once the external accounting system accepts it.
    accounting_sync_status text NOT NULL DEFAULT 'pending'
    CONSTRAINT bookings_accounting_sync_status_valid
    CHECK (accounting_sync_status IN ('pending', 'synced', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE bookings ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookings FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE bookings FROM anon, authenticated;
GRANT SELECT, INSERT ON TABLE bookings TO service_role;

-- Supabase's own default ACL grants ALL privileges, on every column, to
-- service_role on every new table regardless of what is explicitly GRANTed
-- — confirmed empirically for quotes/audit_log, migration 0013's comment.
-- An unqualified `GRANT UPDATE ON TABLE bookings TO service_role` would sit
-- on top of that default and cover every column including
-- customer_phone/full_name/nationality, silently recreating the exact gap
-- 0013 closed for quotes. Revoking the wholesale grant and re-granting
-- UPDATE on only the three operational columns is what actually makes the
-- personal-data columns unreachable by a plain UPDATE — the narrow
-- erasure function below is the only path left to them, same as quotes.
-- DELETE/TRUNCATE/REFERENCES/TRIGGER: no service_role code path has a
-- legitimate reason to use any of these on an accounting record, so all
-- four are revoked outright rather than narrowed. Column-level UPDATE and
-- the outright revokes are both verifiable only against a real Supabase
-- project, not locally: tests/conftest.py's service_role carries no
-- default grant to begin with (same local/Supabase divergence 0013's own
-- comment documents), so locally the column-level GRANT below is the only
-- UPDATE privilege service_role has at all — still meaningful to test
-- locally (a bare UPDATE of customer_phone fails there too), just for a
-- different reason than in Supabase.
REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLE bookings FROM service_role;
GRANT UPDATE (status, payment_status, accounting_sync_status) ON TABLE bookings TO service_role;

-- Right to erasure for conversations: deletes the conversation row, which
-- cascades to its messages and escalations (both ON DELETE CASCADE above).
-- Same SECURITY DEFINER + fixed search_path pattern as
-- quotes_erase_customer_phone (migration 0013); migration 0012's global
-- function-default lockdown already denies EXECUTE to anon/authenticated by
-- default, so the one explicit GRANT below is the only access path.
CREATE FUNCTION conversations_erase_customer(target_phone text) RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    WITH erased AS (
        DELETE FROM conversations
        WHERE customer_phone = target_phone
        RETURNING 1
    )
    SELECT count(*) FROM erased
$$;

GRANT EXECUTE ON FUNCTION conversations_erase_customer(text) TO service_role;

-- Right to erasure for bookings: same shape as quotes_erase_customer_phone
-- (migration 0013) — sets only the three personal-data columns to NULL,
-- never status/payment_status/accounting_sync_status/hold_id/quote_id, and
-- never deletes the row. A blanket UPDATE grant would have let any
-- service_role caller touch those financial columns by accident, which is
-- exactly the gap 0013 closed for quotes; this table never opens it in the
-- first place.
CREATE FUNCTION bookings_erase_customer(target_phone text) RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
    WITH erased AS (
        UPDATE bookings
        SET customer_phone = NULL, full_name = NULL, nationality = NULL
        WHERE customer_phone = target_phone
        RETURNING 1
    )
    SELECT count(*) FROM erased
$$;

GRANT EXECUTE ON FUNCTION bookings_erase_customer(text) TO service_role;
