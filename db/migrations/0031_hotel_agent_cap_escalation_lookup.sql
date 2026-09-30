-- hotel_agent gains read access to three more columns of escalations, so the
-- webhook can tell whether a cap already escalated this number today (owner
-- decision 2026-09-30, ARCHITECTURE.md §7): a number blocked again the same
-- Asia/Riyadh day by any of CLAUDE.md §9's four caps gets the fallback
-- message only, not a second escalation.
--
-- Column-scoped, like 0027's GRANT SELECT (id): the lookup
-- (services/agent/llm/caps.py, find_todays_cap_escalation) filters on these
-- three and returns id, and nothing else. notes, assigned_to and the
-- responded/resolved timestamps stay hidden from the agent. No new policy:
-- 0027's escalations_agent_select already covers every row for SELECT.

GRANT SELECT (reason, customer_phone, opened_at)
ON TABLE escalations TO hotel_agent;
