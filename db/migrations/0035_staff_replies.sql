-- Staff notification step 3 (owner decisions 2026-10-02, ARCHITECTURE.md
-- §7 "إبلاغ الموظفين بالتصعيدات"): the staff member holding a takeover
-- writes to the customer from the dashboard.
--
-- One row per staff reply. The dashboard inserts it through the signed-in
-- user's own session (staff_queue_reply, SECURITY INVOKER as in 0034, so
-- the policies below decide), then asks the agent to send it by id: the
-- agent claims the row once, sends its body through the output guard's
-- staff-reply mode and records the outcome, exactly as it does for the
-- takeover acknowledgement. No request content ever becomes message text;
-- the text the agent sends is the row's body, written under the author's
-- own identity.
--
-- Only the holder of the active takeover may write (owner decision
-- 2026-10-02: an admin who needs to reply ends the takeover and takes it
-- over). Rows are never updated by staff and never deleted (only erasure
-- cascades them away with their conversation). A failed send is not
-- retried: it may have reached the customer anyway, so staff write again.
--
-- No customer_phone column, as conversation_takeovers (0034): the row is
-- staff-written text reached through conversation_id, which erasure
-- cascades, and a sent reply is copied into messages, which carries
-- customer_phone for export.
--
-- Every write policy is paired with a FOR SELECT policy over the same rows
-- (CLAUDE.md rule 11).

CREATE TABLE staff_replies (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    takeover_id bigint NOT NULL REFERENCES conversation_takeovers (id) ON DELETE CASCADE,
    conversation_id bigint NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    -- No ON DELETE, as conversation_takeovers.taken_over_by (0034).
    sent_by uuid NOT NULL REFERENCES app_users (id),
    body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Claimed once, then either sent or failed: claiming before sending is
    -- what keeps a double call from sending it twice.
    claimed_at timestamptz,
    sent_at timestamptz,
    failed_at timestamptz,
    failure_reason text,
    CONSTRAINT staff_replies_body_not_blank CHECK (btrim(body) <> ''),
    -- WhatsApp's limit for a text message body.
    CONSTRAINT staff_replies_body_within_whatsapp_limit CHECK (char_length(body) <= 4096),
    CONSTRAINT staff_replies_outcome_after_claim CHECK (
        (sent_at IS NULL OR claimed_at IS NOT NULL)
        AND (failed_at IS NULL OR claimed_at IS NOT NULL)
        AND (sent_at IS NULL OR failed_at IS NULL)
    ),
    CONSTRAINT staff_replies_failure_has_reason CHECK (
        (failed_at IS NULL AND failure_reason IS NULL)
        OR (failed_at IS NOT NULL AND failure_reason IN ('outside_window', 'send_failed'))
    )
);

CREATE INDEX staff_replies_conversation_id ON staff_replies (conversation_id);

ALTER TABLE staff_replies ENABLE ROW LEVEL SECURITY;
ALTER TABLE staff_replies FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE staff_replies FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE staff_replies TO service_role;
REVOKE TRUNCATE, REFERENCES, TRIGGER ON TABLE staff_replies FROM service_role;

-- ---------------------------------------------------------------------------
-- Dashboard: every active admin and sales user sees every staff reply, as
-- every takeover (0034); only the holder of an active takeover writes one,
-- in their own name, for that takeover's conversation.
-- ---------------------------------------------------------------------------

GRANT SELECT ON TABLE staff_replies TO authenticated;
-- created_at is not granted: it can only be its DEFAULT.
GRANT INSERT (takeover_id, conversation_id, sent_by, body)
ON TABLE staff_replies TO authenticated;

CREATE POLICY staff_replies_select_for_active_users ON staff_replies
FOR SELECT TO authenticated
USING (current_app_role() IS NOT NULL);

CREATE POLICY staff_replies_insert_by_holder ON staff_replies
FOR INSERT TO authenticated
WITH CHECK (
    current_app_role() IS NOT NULL
    AND sent_by = auth.uid()
    AND EXISTS (
        SELECT FROM conversation_takeovers AS t
        WHERE
            t.id = staff_replies.takeover_id
            AND t.conversation_id = staff_replies.conversation_id
            AND t.taken_over_by = auth.uid()
            AND t.ended_at IS NULL
    )
);

-- ---------------------------------------------------------------------------
-- Agent (hotel_agent, 0027): reads a reply to send it, and records the
-- claim and the outcome. Column-scoped: it never sees who wrote it (the
-- amounts audit below reads that as the function's owner).
-- ---------------------------------------------------------------------------

GRANT SELECT (
    id,
    takeover_id,
    conversation_id,
    body,
    created_at,
    claimed_at,
    sent_at,
    failed_at,
    failure_reason
)
ON TABLE staff_replies TO hotel_agent;
GRANT UPDATE (claimed_at, sent_at, failed_at, failure_reason)
ON TABLE staff_replies TO hotel_agent;

CREATE POLICY staff_replies_agent_select ON staff_replies
FOR SELECT TO hotel_agent
USING (TRUE);

-- Claim an unclaimed reply while its takeover is still active, or record
-- the outcome of a claimed one -- even if the takeover ended while it was
-- being sent, as the acknowledgement (0034).
CREATE POLICY staff_replies_agent_send ON staff_replies
FOR UPDATE TO hotel_agent
USING (
    (
        claimed_at IS NULL
        AND EXISTS (
            SELECT FROM conversation_takeovers AS t
            WHERE t.id = staff_replies.takeover_id AND t.ended_at IS NULL
        )
    )
    OR (claimed_at IS NOT NULL AND sent_at IS NULL AND failed_at IS NULL)
)
WITH CHECK (TRUE);

-- ---------------------------------------------------------------------------
-- The sent reply in the conversation: the agent records it as an outbound
-- message linked to its staff reply, so the dashboard can show who wrote
-- it. At most one message per reply.
-- ---------------------------------------------------------------------------

ALTER TABLE messages
ADD COLUMN staff_reply_id bigint REFERENCES staff_replies (id) ON DELETE CASCADE,
ADD CONSTRAINT messages_staff_reply_is_outbound
CHECK (staff_reply_id IS NULL OR direction = 'outbound');

CREATE UNIQUE INDEX messages_staff_reply_id_unique ON messages (staff_reply_id)
WHERE staff_reply_id IS NOT NULL;

GRANT INSERT (staff_reply_id) ON TABLE messages TO hotel_agent;
GRANT SELECT (staff_reply_id) ON TABLE messages TO authenticated;

-- ---------------------------------------------------------------------------
-- The dashboard's write path. SECURITY INVOKER: the policies above decide.
-- The explicit checks only turn a refusal the policies would make anyway
-- into an error the dashboard can name.
-- ---------------------------------------------------------------------------

-- Queues one reply from the caller to the conversation they hold; returns
-- its id, which the dashboard then passes to the agent to send.
CREATE FUNCTION staff_queue_reply(target_conversation_id bigint, reply_body text)
RETURNS bigint
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
    active_takeover_id bigint;
    holder uuid;
    new_reply_id bigint;
BEGIN
    IF current_app_role() IS NULL THEN
        RAISE EXCEPTION 'not permitted to reply to customers' USING ERRCODE = '42501';
    END IF;

    SELECT t.id, t.taken_over_by INTO active_takeover_id, holder
    FROM conversation_takeovers AS t
    WHERE t.conversation_id = target_conversation_id AND t.ended_at IS NULL;

    IF active_takeover_id IS NULL THEN
        RAISE EXCEPTION 'conversation is not taken over' USING ERRCODE = 'P0002';
    END IF;
    IF holder <> auth.uid() THEN
        RAISE EXCEPTION 'conversation is taken over by another staff member'
        USING ERRCODE = '42501';
    END IF;

    INSERT INTO staff_replies (takeover_id, conversation_id, sent_by, body)
    VALUES (active_takeover_id, target_conversation_id, auth.uid(), reply_body)
    RETURNING id INTO new_reply_id;
    RETURN new_reply_id;
END;
$$;

GRANT EXECUTE ON FUNCTION staff_queue_reply(bigint, text) TO authenticated;

-- ---------------------------------------------------------------------------
-- The amounts audit (ARCHITECTURE.md §7, step 3 item 4): every staff reply
-- that states an amount is written to audit_log -- who, when, the amounts,
-- the conversation -- before it is sent. The agent finds the amounts with
-- the output guard's own extraction (one way to find an amount), so it is
-- the writer; but neither backend role reads or writes audit_log directly
-- (0027), so this one narrow SECURITY DEFINER function does the insert.
-- It takes only the reply id and the amounts: the author and the
-- conversation come from the reply row itself, so the agent cannot name
-- anyone else, and it refuses a reply that is not claimed-and-unsent or
-- already audited. The function owner holds BYPASSRLS (0016's check).
-- ---------------------------------------------------------------------------

CREATE FUNCTION staff_reply_record_amounts(target_reply_id bigint, stated_amounts jsonb)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    author uuid;
    reply_conversation_id bigint;
BEGIN
    IF
        stated_amounts IS NULL
        OR jsonb_typeof(stated_amounts) <> 'array'
        OR jsonb_array_length(stated_amounts) = 0
    THEN
        RAISE EXCEPTION 'stated_amounts must be a non-empty array' USING ERRCODE = '22023';
    END IF;

    SELECT r.sent_by, r.conversation_id INTO author, reply_conversation_id
    FROM staff_replies AS r
    WHERE
        r.id = target_reply_id
        AND r.claimed_at IS NOT NULL
        AND r.sent_at IS NULL
        AND r.failed_at IS NULL;

    IF author IS NULL THEN
        RAISE EXCEPTION 'staff reply is not claimed and unsent' USING ERRCODE = 'P0002';
    END IF;
    IF EXISTS (
        SELECT FROM audit_log AS a
        WHERE
            a.table_name = 'staff_replies'
            AND a.row_id = target_reply_id::text
            AND a.column_name = 'amounts'
    ) THEN
        RAISE EXCEPTION 'staff reply amounts already recorded' USING ERRCODE = '23505';
    END IF;

    INSERT INTO audit_log (table_name, row_id, column_name, old_value, new_value, changed_by)
    VALUES (
        'staff_replies',
        target_reply_id::text,
        'amounts',
        NULL,
        jsonb_build_object(
            'conversation_id', reply_conversation_id, 'amounts', stated_amounts
        ),
        author
    );
END;
$$;

GRANT EXECUTE ON FUNCTION staff_reply_record_amounts(bigint, jsonb) TO hotel_agent;

-- A staff member's stated amount is a price the customer was told, not a
-- cost or margin figure, so every admin may read it: widens 0022's
-- allow-list by this one (table_name, column_name) pair, restating it whole.
ALTER POLICY audit_log_select_admin_only ON audit_log
USING (
    current_app_role() = 'admin'
    AND (
        current_user_can_view_cost()
        OR (table_name, column_name) IN (
            ('app_users', 'app_role'),
            ('app_users', 'can_view_cost'),
            ('price_rules', 'demand_curve'),
            ('price_rules', 'is_active'),
            ('price_overrides', 'ask_price_override'),
            ('price_overrides', 'min_allowed_override'),
            ('price_overrides', 'expires_at'),
            ('staff_replies', 'amounts')
        )
    )
);

-- Live updates, as 0033 and 0034: a reply's outcome shows on every screen.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE staff_replies;
    END IF;
END
$$;
