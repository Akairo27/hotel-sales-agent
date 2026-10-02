-- Staff notification step 2a (owner decisions 2026-10-01 and 2026-10-02,
-- ARCHITECTURE.md §7 "إبلاغ الموظفين بالتصعيدات"): a staff member takes
-- over a customer's conversation from the dashboard, and later resolves it
-- or hands it back to the bot. Everything acts on the whole conversation,
-- never on one escalation.
--
-- One row per takeover. The partial unique index allows one active
-- takeover per conversation, so two staff members pressing "take over" at
-- the same moment cannot both win: staff_take_over_conversation inserts
-- with ON CONFLICT DO NOTHING and the loser gets the holder back. The rule
-- lives in the database, not in an application check, the same reasoning
-- as inventory_never_oversold. Rows are never deleted (only erasure
-- cascades them away with their conversation) and an ended row can no
-- longer be changed, so the table is also the audit trail: who took over
-- and when, who ended it, how and when.
--
-- While a takeover is active the agent stays silent apart from one fixed
-- acknowledgement to the customer (services/agent/takeover.py). The agent
-- reads the takeover state and records that acknowledgement; it never
-- creates or ends a takeover.
--
-- escalations.assigned_to and responded_at (0024) stay unused: the active
-- takeover is the one record of who holds a customer, including for an
-- escalation opened after the takeover began.
--
-- Write access is SECURITY INVOKER throughout, so the policies below decide
-- every write and the dashboard calls the functions through the signed-in
-- user's own session. Every write policy is paired with a FOR SELECT policy
-- over the same rows (CLAUDE.md rule 11).

CREATE TABLE conversation_takeovers (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Erasure (conversations_erase_customer, 0024) deletes the
    -- conversation and these rows with it. No customer_phone of its own:
    -- the row holds staff actions and times, no customer data.
    conversation_id bigint NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    -- No ON DELETE: like audit_log.changed_by (0011), a staff member who
    -- appears in the history cannot be deleted out from under it.
    taken_over_by uuid NOT NULL REFERENCES app_users (id),
    taken_over_at timestamptz NOT NULL DEFAULT now(),
    ended_by uuid REFERENCES app_users (id),
    ended_at timestamptz,
    outcome text,
    -- The acknowledgement to the customer: claimed once, then either sent
    -- or failed. Claiming before sending is what keeps a double call from
    -- sending it twice.
    ack_claimed_at timestamptz,
    ack_sent_at timestamptz,
    ack_failed_at timestamptz,
    CONSTRAINT conversation_takeovers_end_complete CHECK (
        (ended_at IS NULL AND ended_by IS NULL AND outcome IS NULL)
        OR (
            ended_at IS NOT NULL
            AND ended_by IS NOT NULL
            AND outcome IN ('resolved', 'handed_back')
        )
    ),
    CONSTRAINT conversation_takeovers_ended_after_taken
    CHECK (ended_at IS NULL OR ended_at >= taken_over_at),
    CONSTRAINT conversation_takeovers_ack_after_claim CHECK (
        (ack_sent_at IS NULL OR ack_claimed_at IS NOT NULL)
        AND (ack_failed_at IS NULL OR ack_claimed_at IS NOT NULL)
        AND (ack_sent_at IS NULL OR ack_failed_at IS NULL)
    )
);

CREATE UNIQUE INDEX conversation_takeovers_one_active
ON conversation_takeovers (conversation_id)
WHERE ended_at IS NULL;

ALTER TABLE conversation_takeovers ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_takeovers FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE conversation_takeovers FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE conversation_takeovers TO service_role;
REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLE conversation_takeovers FROM service_role;

-- ---------------------------------------------------------------------------
-- Dashboard: every active admin and sales user (current_app_role() IS NOT
-- NULL, 0010) sees every takeover, as they see every escalation (0033).
-- ---------------------------------------------------------------------------

GRANT SELECT ON TABLE conversation_takeovers TO authenticated;
-- taken_over_at is not granted: it can only be its DEFAULT.
GRANT INSERT (conversation_id, taken_over_by) ON TABLE conversation_takeovers TO authenticated;
GRANT UPDATE (ended_by, ended_at, outcome) ON TABLE conversation_takeovers TO authenticated;

CREATE POLICY conversation_takeovers_select_for_active_users ON conversation_takeovers
FOR SELECT TO authenticated
USING (current_app_role() IS NOT NULL);

-- A staff member takes over for themselves only, and only a conversation
-- that has an open escalation: the dashboard shows no other.
CREATE POLICY conversation_takeovers_insert_own ON conversation_takeovers
FOR INSERT TO authenticated
WITH CHECK (
    current_app_role() IS NOT NULL
    AND taken_over_by = auth.uid()
    AND EXISTS (
        SELECT FROM escalations AS e
        WHERE
            e.conversation_id = conversation_takeovers.conversation_id
            AND e.resolved_at IS NULL
    )
);

-- The holder or any admin ends an active takeover (owner decision D4: a
-- holder who went off shift or was deactivated must not leave the bot
-- silent for good). The end is stamped with the caller and the statement's
-- own time, so a direct update cannot backdate it or name someone else.
-- Once ended_at is set the row no longer matches USING: ended rows are
-- immutable.
CREATE POLICY conversation_takeovers_end_by_holder_or_admin ON conversation_takeovers
FOR UPDATE TO authenticated
USING (
    ended_at IS NULL
    AND current_app_role() IS NOT NULL
    AND (taken_over_by = auth.uid() OR current_app_role() = 'admin')
)
WITH CHECK (ended_by = auth.uid() AND ended_at = now());

-- Resolving closes escalations. Any active staff member may close those of
-- a conversation nobody else holds (owner decision D2: Resolve also works
-- without a takeover); while someone else holds it, only that holder or an
-- admin may. Paired with 0033's escalations_select_for_active_users, which
-- covers every row for the same users. Closing is stamped with now() for
-- the same reason as the end of a takeover.
GRANT UPDATE (resolved_at) ON TABLE escalations TO authenticated;

CREATE POLICY escalations_resolve_for_active_users ON escalations
FOR UPDATE TO authenticated
USING (
    resolved_at IS NULL
    AND current_app_role() IS NOT NULL
    AND (
        current_app_role() = 'admin'
        OR NOT EXISTS (
            SELECT FROM conversation_takeovers AS t
            WHERE
                t.conversation_id = escalations.conversation_id
                AND t.ended_at IS NULL
                AND t.taken_over_by <> auth.uid()
        )
    )
)
WITH CHECK (resolved_at = now());

-- ---------------------------------------------------------------------------
-- Agent (hotel_agent, 0027): reads whether a conversation is taken over,
-- and records the acknowledgement. Column-scoped: it never sees which staff
-- member holds a conversation, and writes only the three ack columns.
-- ---------------------------------------------------------------------------

GRANT SELECT (
    id,
    conversation_id,
    taken_over_at,
    ended_at,
    ack_claimed_at,
    ack_sent_at,
    ack_failed_at
)
ON TABLE conversation_takeovers TO hotel_agent;
GRANT UPDATE (ack_claimed_at, ack_sent_at, ack_failed_at)
ON TABLE conversation_takeovers TO hotel_agent;

CREATE POLICY conversation_takeovers_agent_select ON conversation_takeovers
FOR SELECT TO hotel_agent
USING (TRUE);

-- Claim the acknowledgement of an active takeover, or record the outcome
-- of a claimed one -- even if the takeover ended while it was being sent.
CREATE POLICY conversation_takeovers_agent_ack ON conversation_takeovers
FOR UPDATE TO hotel_agent
USING (
    (ended_at IS NULL AND ack_claimed_at IS NULL)
    OR (ack_claimed_at IS NOT NULL AND ack_sent_at IS NULL AND ack_failed_at IS NULL)
)
WITH CHECK (TRUE);

-- ---------------------------------------------------------------------------
-- The dashboard's write path. SECURITY INVOKER: the policies above decide.
-- The explicit checks only turn a refusal the policies would make anyway
-- into an error the dashboard can name.
-- ---------------------------------------------------------------------------

-- Returns one row: won = true with the new takeover, or won = false with
-- the takeover that got there first (possibly the caller's own). No row at
-- all means that takeover ended between the two statements; the dashboard
-- asks the user to try again.
CREATE FUNCTION staff_take_over_conversation(target_conversation_id bigint)
RETURNS TABLE (takeover_id bigint, won boolean, holder_id uuid, holder_since timestamptz)
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
    new_takeover_id bigint;
BEGIN
    IF current_app_role() IS NULL THEN
        RAISE EXCEPTION 'not permitted to take over conversations' USING ERRCODE = '42501';
    END IF;
    IF NOT EXISTS (
        SELECT FROM escalations AS e
        WHERE e.conversation_id = target_conversation_id AND e.resolved_at IS NULL
    ) THEN
        RAISE EXCEPTION 'conversation has no open escalation' USING ERRCODE = 'P0002';
    END IF;

    INSERT INTO conversation_takeovers (conversation_id, taken_over_by)
    VALUES (target_conversation_id, auth.uid())
    ON CONFLICT (conversation_id) WHERE ended_at IS NULL DO NOTHING
    RETURNING id INTO new_takeover_id;

    IF new_takeover_id IS NOT NULL THEN
        RETURN QUERY SELECT new_takeover_id, true, auth.uid(), now();
        RETURN;
    END IF;
    RETURN QUERY
    SELECT t.id, false, t.taken_over_by, t.taken_over_at
    FROM conversation_takeovers AS t
    WHERE t.conversation_id = target_conversation_id AND t.ended_at IS NULL;
END;
$$;

-- Resolve (close_outcome 'resolved') or hand back to the bot
-- ('handed_back'): ends the active takeover, if any, and closes every open
-- escalation of the conversation in the same transaction. Hand back needs
-- an active takeover; resolve does not. Neither sends the customer
-- anything (owner decision D2). Returns how many escalations it closed.
CREATE FUNCTION staff_close_conversation(target_conversation_id bigint, close_outcome text)
RETURNS integer
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
    holder uuid;
    closed_count integer;
BEGIN
    IF current_app_role() IS NULL THEN
        RAISE EXCEPTION 'not permitted to close conversations' USING ERRCODE = '42501';
    END IF;
    IF close_outcome IS NULL OR close_outcome NOT IN ('resolved', 'handed_back') THEN
        RAISE EXCEPTION 'unknown close outcome' USING ERRCODE = '22023';
    END IF;

    SELECT t.taken_over_by INTO holder
    FROM conversation_takeovers AS t
    WHERE t.conversation_id = target_conversation_id AND t.ended_at IS NULL;

    IF holder IS NULL AND close_outcome = 'handed_back' THEN
        RAISE EXCEPTION 'conversation is not taken over' USING ERRCODE = 'P0002';
    END IF;
    IF holder IS NOT NULL THEN
        IF holder <> auth.uid() AND current_app_role() <> 'admin' THEN
            RAISE EXCEPTION 'conversation is taken over by another staff member'
            USING ERRCODE = '42501';
        END IF;
        UPDATE conversation_takeovers
        SET ended_at = now(), ended_by = auth.uid(), outcome = close_outcome
        WHERE conversation_id = target_conversation_id AND ended_at IS NULL;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'takeover already ended' USING ERRCODE = 'P0002';
        END IF;
    END IF;

    UPDATE escalations
    SET resolved_at = now()
    WHERE conversation_id = target_conversation_id AND resolved_at IS NULL;
    GET DIAGNOSTICS closed_count = ROW_COUNT;
    RETURN closed_count;
END;
$$;

GRANT EXECUTE ON FUNCTION staff_take_over_conversation(bigint) TO authenticated;
GRANT EXECUTE ON FUNCTION staff_close_conversation(bigint, text) TO authenticated;

-- ---------------------------------------------------------------------------
-- Staff names, so every staff member can see who holds a conversation
-- (owner decision D7). app_users itself stays readable to its own row and
-- to admins only (0010): a sales user must not read others' roles or cost
-- visibility. Same owner-privileged view shape as price_rules_for_dashboard
-- (0018): the view reads app_users as its owner, so its WHERE restates who
-- may read it. Deactivated staff are listed too -- they still appear in
-- the takeover history.
-- ---------------------------------------------------------------------------

CREATE VIEW staff_names_for_dashboard AS
SELECT
    id,
    full_name
FROM app_users
WHERE current_app_role() IS NOT NULL;

GRANT SELECT ON staff_names_for_dashboard TO authenticated;

-- Live updates, as 0033: a takeover on one screen shows on every other.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE conversation_takeovers;
    END IF;
END
$$;
