-- A booking request is escalated at most once per quote (owner decision
-- 2026-09-30, ARCHITECTURE.md §7). The agent's request_booking_follow_up
-- tool (services/agent/llm/booking_follow_up.py) opens a booking_requested
-- escalation only after the customer explicitly says yes to a quoted
-- stay; a customer who says yes twice, even in two messages processed at
-- the same moment, must not produce a second one.
--
-- escalations had no link to a quote, and the agent cannot read notes, so
-- a check-then-insert in code could neither find an earlier request nor
-- avoid the race between two of them. quote_id plus a partial unique index
-- makes the database itself refuse the second row; the tool inserts with
-- ON CONFLICT DO NOTHING. quote_id stays NULL for every other reason, and
-- the index covers booking_requested rows only.
--
-- Column-scoped grants, as in 0027 and 0031: the agent writes quote_id
-- and reads it back for the conflict check, nothing more. No new policy:
-- 0027's escalations_agent_select and escalations_agent_insert already
-- cover every row.

ALTER TABLE escalations
ADD COLUMN quote_id bigint REFERENCES quotes (id);

CREATE UNIQUE INDEX escalations_one_booking_request_per_quote
ON escalations (quote_id)
WHERE reason = 'booking_requested';

GRANT INSERT (quote_id) ON TABLE escalations TO hotel_agent;
GRANT SELECT (quote_id) ON TABLE escalations TO hotel_agent;
