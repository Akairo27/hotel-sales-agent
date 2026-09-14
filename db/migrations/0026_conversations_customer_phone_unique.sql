-- Phase 4, webhook PR: find-or-create needs a unique constraint on
-- conversations.customer_phone. A WhatsApp thread has no customer-visible
-- "new conversation" concept — one phone number always maps to exactly one
-- conversation row, reused indefinitely, so concession_count/turn_count
-- persist across the whole relationship instead of resetting on every gap
-- in messaging (flagged as an open design point in the phase 4 plan,
-- resolved here).
--
-- Without this constraint, two webhook deliveries for the same number
-- arriving concurrently could each find zero existing rows and both
-- INSERT, producing two conversation rows for one customer. The unique
-- index lets the webhook's find-or-create use a single atomic
-- INSERT ... ON CONFLICT (customer_phone) instead of a
-- SELECT-then-maybe-INSERT that races under concurrent delivery — the
-- same reasoning CLAUDE.md rule 3 gives for inventory, applied here by
-- analogy since conversations is not itself a financial-loss surface.
ALTER TABLE conversations
ADD CONSTRAINT conversations_customer_phone_unique UNIQUE (customer_phone);
