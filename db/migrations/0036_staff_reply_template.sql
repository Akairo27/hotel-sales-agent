-- Staff notification step 3, PR C (owner decisions 2026-10-02,
-- ARCHITECTURE.md §7): the re-engagement template. Outside WhatsApp's
-- 24-hour window a free-text reply is refused, so the staff member sends
-- the approved utility template instead ("we tried to reach you about your
-- request at {{1}} ..."). It reuses the staff_replies row, so it is
-- claimed once, audited, linked to its outbound message and shown in the
-- conversation exactly like a staff reply; only what is sent differs.
--
-- What this adds, and nothing more:
--   * kind: 'text' (every existing row) or 'template'.
--   * template_hotel_id: the hotel the template names ({{1}}), chosen by
--     the dashboard (the conversation's latest quote's hotel, else the open
--     escalation's) and only for a template; NULL means the variant with no
--     hotel name. The agent reads only the hotel's name (0030's grant), so it
--     needs no new access to quotes or escalation notes.
--   * 'window_open' as a third failure reason: a template is refused while
--     the customer's last message is under 24 hours old (free text is the
--     right tool then). This replaces the failure-reason CHECK with the same
--     rule plus that one value; no existing row can violate it.
--   * staff_queue_template_reply: the dashboard's write path for a template,
--     SECURITY INVOKER as staff_queue_reply (0035), so the existing policies
--     decide who may write.
--
-- Every write policy still has its FOR SELECT pair (CLAUDE.md rule 11):
-- 0035's policies are unchanged and cover the new columns, which sit on the
-- same rows.

ALTER TABLE staff_replies
ADD COLUMN kind text NOT NULL DEFAULT 'text',
ADD COLUMN template_hotel_id bigint REFERENCES hotels (id) ON DELETE SET NULL,
ADD CONSTRAINT staff_replies_kind_known CHECK (kind IN ('text', 'template')),
ADD CONSTRAINT staff_replies_template_hotel_only_for_template CHECK (
    template_hotel_id IS NULL OR kind = 'template'
);

ALTER TABLE staff_replies
DROP CONSTRAINT staff_replies_failure_has_reason,
ADD CONSTRAINT staff_replies_failure_has_reason CHECK (
    (failed_at IS NULL AND failure_reason IS NULL)
    -- IS NOT NULL is explicit: a NULL reason would make IN yield NULL,
    -- which a CHECK accepts.
    OR (
        failed_at IS NOT NULL
        AND failure_reason IS NOT NULL
        AND failure_reason IN ('outside_window', 'send_failed', 'window_open')
    )
);

GRANT INSERT (kind, template_hotel_id) ON TABLE staff_replies TO authenticated;
GRANT SELECT (kind, template_hotel_id) ON TABLE staff_replies TO hotel_agent;

-- Queues one re-engagement template from the caller to the conversation they
-- hold; returns its id, which the dashboard then passes to the agent to
-- send. The body is a fixed placeholder (the text the customer gets is the
-- approved template, rendered by the agent and recorded in messages).
CREATE FUNCTION staff_queue_template_reply(
    target_conversation_id bigint, hotel_for_template bigint
)
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

    INSERT INTO staff_replies (
        takeover_id, conversation_id, sent_by, body, kind, template_hotel_id
    )
    VALUES (
        active_takeover_id, target_conversation_id, auth.uid(),
        'قالب إعادة التواصل', 'template', hotel_for_template
    )
    RETURNING id INTO new_reply_id;
    RETURN new_reply_id;
END;
$$;

GRANT EXECUTE ON FUNCTION staff_queue_template_reply(bigint, bigint) TO authenticated;
