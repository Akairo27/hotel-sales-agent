-- Dedicated, least-privilege database roles for the two backend processes:
-- hotel_agent (the WhatsApp webhook) and hotel_worker (the scheduled worker).
-- See docs/plans/role-fix.md for the plan and the reasoning.
--
-- Until this migration the backend connected as `postgres`, which owns every
-- table, has BYPASSRLS, and can therefore UPDATE, DELETE and TRUNCATE the
-- append-only quotes and audit_log tables. Migration 0013's lockdown revokes
-- those privileges from service_role only, and nothing connects as
-- service_role, so it bound nothing. These are the roles that lockdown was
-- meant to bind: they own nothing, cannot bypass RLS, and hold exactly the
-- privileges the code in services/ uses today.
--
-- No password is set here, and none ever is in a migration: the owner sets
-- each role's password out of band (ALTER ROLE ... PASSWORD ...), so no
-- credential enters this repository. Until then neither role can log in
-- with a password. No VALID UNTIL either: an expired role breaks
-- authentication through Supabase's connection pooler.
--
-- Role creation is guarded because roles are cluster-wide, not per-database:
-- they outlive the schema resets the test suite performs. The attributes are
-- the CREATE ROLE defaults (NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
-- NOREPLICATION, NOBYPASSRLS) plus LOGIN and NOINHERIT; the integration
-- tests read pg_roles to prove it rather than trusting this comment.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hotel_agent') THEN
        CREATE ROLE hotel_agent LOGIN NOINHERIT;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'hotel_worker') THEN
        CREATE ROLE hotel_worker LOGIN NOINHERIT;
    END IF;
END
$$;

-- CLAUDE.md section 8: every external call has a timeout, and a hanging call
-- must not hang a customer conversation. A role-level default applies to
-- every statement the role runs, including ones no application code thought
-- to bound. The agent's statements are short reads and single-row writes;
-- the worker's release loop gets more headroom.
ALTER ROLE hotel_agent SET statement_timeout = '10s';
ALTER ROLE hotel_worker SET statement_timeout = '30s';

GRANT USAGE ON SCHEMA public TO hotel_agent, hotel_worker;

-- ---------------------------------------------------------------------------
-- hotel_agent
--
-- RLS is enabled and forced on every table below and there is no policy for
-- these roles until this migration, so deny-by-default holds for anything not
-- listed. The backend legitimately reads and writes every row of the tables
-- it uses, so its policies cannot narrow which rows: `USING (true)` is the
-- honest statement of that. What they add is fail-closed behaviour for a new
-- table or a new command, while the column-level GRANTs below do the real
-- narrowing. Every INSERT or UPDATE policy has a paired SELECT policy
-- (CLAUDE.md rule 11): the upsert on conversations and the RETURNING clauses
-- fail without one.
-- ---------------------------------------------------------------------------

-- conversations: the webhook upserts a row per phone number
-- (INSERT ... ON CONFLICT (customer_phone) DO UPDATE ... RETURNING id), and
-- the caps and session code update its counters and last_message_at.
GRANT SELECT ON TABLE conversations TO hotel_agent;
GRANT INSERT (customer_phone) ON TABLE conversations TO hotel_agent;
GRANT UPDATE (
    customer_phone, turn_count, active_quote_id, concession_count, last_message_at
) ON TABLE conversations TO hotel_agent;

CREATE POLICY conversations_agent_select ON conversations
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY conversations_agent_insert ON conversations
FOR INSERT TO hotel_agent
WITH CHECK (true);

CREATE POLICY conversations_agent_update ON conversations
FOR UPDATE TO hotel_agent
USING (true)
WITH CHECK (true);

-- messages: inbound and outbound logging (idempotent on whatsapp_message_id).
-- Append-only from the agent's side: no UPDATE, no DELETE.
GRANT SELECT ON TABLE messages TO hotel_agent;
GRANT INSERT (
    conversation_id, customer_phone, direction, whatsapp_message_id, body
) ON TABLE messages TO hotel_agent;

CREATE POLICY messages_agent_select ON messages
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY messages_agent_insert ON messages
FOR INSERT TO hotel_agent
WITH CHECK (true);

-- escalations: the agent opens them and reads back only the new id
-- (INSERT ... RETURNING id). Resolving one is a human's job, not the agent's.
GRANT INSERT (conversation_id, customer_phone, reason, notes)
ON TABLE escalations TO hotel_agent;
GRANT SELECT (id) ON TABLE escalations TO hotel_agent;

CREATE POLICY escalations_agent_select ON escalations
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY escalations_agent_insert ON escalations
FOR INSERT TO hotel_agent
WITH CHECK (true);

-- token_usage: the daily and per-conversation spend caps read it and each
-- model call appends to it.
GRANT SELECT ON TABLE token_usage TO hotel_agent;
GRANT INSERT (
    conversation_id, customer_phone, prompt_tokens, candidates_tokens, total_tokens, created_at
) ON TABLE token_usage TO hotel_agent;

CREATE POLICY token_usage_agent_select ON token_usage
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY token_usage_agent_insert ON token_usage
FOR INSERT TO hotel_agent
WITH CHECK (true);

-- quotes: append-only. The agent inserts every price it quotes and the output
-- guard reads them back. This is the grant migration 0013's lockdown was
-- written to enforce, and now it does: no UPDATE, no DELETE, no TRUNCATE.
-- The customer-erasure function (migration 0013) stays ungranted; no code in
-- services/ calls it.
--
-- The two validators are called by the quotes_nights_are_complete CHECK
-- constraint (migration 0009). A role that writes a table whose CHECK calls a
-- function needs EXECUTE on it: price_rules' validators had to be granted to
-- authenticated for the same reason, and only postgres and service_role hold
-- these two today.
GRANT SELECT ON TABLE quotes TO hotel_agent;
GRANT INSERT (
    hotel_id, room_type_id, check_in, check_out, rooms, ask_price_total,
    min_allowed_total, nights, negotiation_open, customer_phone, conversation_id
) ON TABLE quotes TO hotel_agent;
GRANT EXECUTE ON FUNCTION quotes_is_valid_night_record(jsonb) TO hotel_agent;
GRANT EXECUTE ON FUNCTION quotes_all_nights_are_complete(jsonb) TO hotel_agent;

CREATE POLICY quotes_agent_select ON quotes
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY quotes_agent_insert ON quotes
FOR INSERT TO hotel_agent
WITH CHECK (true);

-- Read-only inputs to pricing and availability. allotments carries
-- cost_per_night, which the pricing service reads inside the process; the
-- model never sees it (CLAUDE.md rule 2), and that is enforced by what the
-- tool results contain, not by this grant.
GRANT SELECT ON TABLE allotments TO hotel_agent;
GRANT SELECT ON TABLE room_night_inventory TO hotel_agent;
GRANT SELECT ON TABLE seasons TO hotel_agent;
GRANT SELECT ON TABLE price_rules TO hotel_agent;
GRANT SELECT ON TABLE price_overrides TO hotel_agent;

CREATE POLICY allotments_agent_select ON allotments
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY room_night_inventory_agent_select ON room_night_inventory
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY seasons_agent_select ON seasons
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY price_rules_agent_select ON price_rules
FOR SELECT TO hotel_agent
USING (true);

CREATE POLICY price_overrides_agent_select ON price_overrides
FOR SELECT TO hotel_agent
USING (true);

-- ---------------------------------------------------------------------------
-- hotel_worker
--
-- Releases expired holds. release_hold locks the night rows with
-- SELECT ... FOR UPDATE (which needs UPDATE on at least one column) and sets
-- held and reserved together, even when the reserved delta is zero, so both
-- columns need UPDATE. It reads allotments only to join hotel and room type
-- to the night rows, so it gets only the three columns that join uses and
-- cannot read cost_per_night. A future worker job that needs more widens this
-- in its own change.
-- ---------------------------------------------------------------------------

GRANT SELECT ON TABLE holds TO hotel_worker;
GRANT UPDATE (released_at) ON TABLE holds TO hotel_worker;

CREATE POLICY holds_worker_select ON holds
FOR SELECT TO hotel_worker
USING (true);

CREATE POLICY holds_worker_update ON holds
FOR UPDATE TO hotel_worker
USING (true)
WITH CHECK (true);

GRANT SELECT ON TABLE room_night_inventory TO hotel_worker;
GRANT UPDATE (held, reserved) ON TABLE room_night_inventory TO hotel_worker;

CREATE POLICY room_night_inventory_worker_select ON room_night_inventory
FOR SELECT TO hotel_worker
USING (true);

CREATE POLICY room_night_inventory_worker_update ON room_night_inventory
FOR UPDATE TO hotel_worker
USING (true)
WITH CHECK (true);

GRANT SELECT (id, hotel_id, room_type_id) ON TABLE allotments TO hotel_worker;

CREATE POLICY allotments_worker_select ON allotments
FOR SELECT TO hotel_worker
USING (true);
