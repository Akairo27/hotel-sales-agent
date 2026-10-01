-- Staff notification, step 1 (owner decisions 2026-10-01, ARCHITECTURE.md
-- §7): the dashboard's read-only escalations screen. Every active admin and
-- sales user (current_app_role() IS NOT NULL, migration 0010) reads every
-- escalation, and the conversation, messages and quotes of a conversation
-- that has one -- nothing of a conversation that never escalated.
--
-- SELECT only: authenticated gets no INSERT, UPDATE or DELETE here, so
-- CLAUDE.md rule 11's write/read policy pairing is not engaged. Taking over,
-- resolving and replying come in later steps, each with its own migration.
--
-- Column-scoped where a table holds more than the screen shows:
-- conversations' counters and messages' WhatsApp ids stay hidden, and
-- quotes never expose nights (compute.py's audit trail, cost included) or
-- min_allowed_total (the price floor) -- CLAUDE.md rule 2 and §8's cost
-- masking. escalations is readable whole: its notes carry exception types,
-- a blocked reply's text and amounts, booking-claim phrases and stay dates,
-- never a cost or a floor.

GRANT SELECT ON TABLE escalations TO authenticated;

CREATE POLICY escalations_select_for_active_users ON escalations
FOR SELECT TO authenticated
USING (current_app_role() IS NOT NULL);

GRANT SELECT (id, customer_phone, last_message_at)
ON TABLE conversations TO authenticated;

CREATE POLICY conversations_select_escalated_for_active_users ON conversations
FOR SELECT TO authenticated
USING (
    current_app_role() IS NOT NULL
    AND EXISTS (
        SELECT FROM escalations AS e
        WHERE e.conversation_id = conversations.id
    )
);

GRANT SELECT (id, conversation_id, direction, body, created_at)
ON TABLE messages TO authenticated;

CREATE POLICY messages_select_escalated_for_active_users ON messages
FOR SELECT TO authenticated
USING (
    current_app_role() IS NOT NULL
    AND EXISTS (
        SELECT FROM escalations AS e
        WHERE e.conversation_id = messages.conversation_id
    )
);

GRANT SELECT (
    id,
    conversation_id,
    hotel_id,
    room_type_id,
    check_in,
    check_out,
    rooms,
    ask_price_total,
    created_at
)
ON TABLE quotes TO authenticated;

CREATE POLICY quotes_select_escalated_for_active_users ON quotes
FOR SELECT TO authenticated
USING (
    current_app_role() IS NOT NULL
    AND EXISTS (
        SELECT FROM escalations AS e
        WHERE e.conversation_id = quotes.conversation_id
    )
);

-- Live updates for the screen (owner decision 8): Supabase Realtime streams
-- changes of the tables in its supabase_realtime publication, checked per
-- subscriber against the policies above. Every Supabase project has this
-- publication; a plain Postgres (CI, the model-eval workflow) may not, and
-- there the screen falls back to polling, so the statement is skipped.
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_publication WHERE pubname = 'supabase_realtime') THEN
        ALTER PUBLICATION supabase_realtime ADD TABLE escalations, messages;
    END IF;
END
$$;
