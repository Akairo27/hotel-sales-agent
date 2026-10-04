-- Price-sheet import, PR 1, second half: the audit trail and who may reach
-- the three tables migration 0037 created (and left closed to every role).
-- See 0037's header for the design; docs/price-import-plan-draft.md for the
-- plan.
--
-- Every write policy is paired with a FOR SELECT policy over the same rows
-- (CLAUDE.md rule 11). No role is granted DELETE on any of the three.

-- ---------------------------------------------------------------------------
-- Audit: one audit_log row per changed column, as price_overrides (0021).
-- One function for both tables: the columns to audit are the trigger's
-- arguments. Updates only -- who created a batch or a row is on the row.
-- SECURITY DEFINER because the writing role holds no INSERT on audit_log.
-- ---------------------------------------------------------------------------

CREATE FUNCTION rate_import_audit_trigger() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    actor uuid := rate_import_actor_id();
    old_row jsonb := to_jsonb(old);
    new_row jsonb := to_jsonb(new);
    audited_column text;
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'the acting user must be known before changing %', TG_TABLE_NAME;
    END IF;
    FOR argument_index IN 0..TG_NARGS - 1 LOOP
        audited_column := TG_ARGV[argument_index];
        IF old_row -> audited_column IS DISTINCT FROM new_row -> audited_column THEN
            -- NULLIF: a NULL column is the jsonb literal null inside
            -- to_jsonb(row); audit_log stores it as SQL NULL, as 0018 does.
            INSERT INTO audit_log (
                table_name, row_id, column_name, old_value, new_value, changed_by
            )
            VALUES (
                TG_TABLE_NAME,
                new.id::text,
                audited_column,
                NULLIF(old_row -> audited_column, 'null'::jsonb),
                NULLIF(new_row -> audited_column, 'null'::jsonb),
                actor
            );
        END IF;
    END LOOP;
    RETURN new;
END;
$$;

CREATE TRIGGER rate_import_batches_audit
AFTER UPDATE ON rate_import_batches
FOR EACH ROW
EXECUTE FUNCTION rate_import_audit_trigger(
    'status', 'period_end_inclusive', 'period_years_confirmed'
);

CREATE TRIGGER rate_import_rows_audit
AFTER UPDATE ON rate_import_rows
FOR EACH ROW
EXECUTE FUNCTION rate_import_audit_trigger(
    'room_type_id',
    'period_start',
    'period_end',
    'weekday_price_halalas',
    'weekend_price_halalas',
    'is_closed',
    'is_excluded'
);

-- ---------------------------------------------------------------------------
-- Access. 0037 enabled and forced RLS and revoked everything; these are
-- the only grants, each role getting exactly what it needs.
-- ---------------------------------------------------------------------------

-- service_role: no DELETE on any of the three, and nights are append-only
-- for it as for everyone (as quotes, 0013).
GRANT SELECT, INSERT, UPDATE ON TABLE rate_import_batches TO service_role;
GRANT SELECT, INSERT, UPDATE ON TABLE rate_import_rows TO service_role;
GRANT SELECT, INSERT ON TABLE rate_import_nights TO service_role;

-- Dashboard: admins only, for reading as well as writing (ARCHITECTURE.md
-- §4: "ولا يصلها إلا المدير"). Column-scoped writes: an admin names the
-- hotel and price type of a new batch, then changes only its status and
-- the two review choices; every stamp is the guard's.
GRANT SELECT ON TABLE rate_import_batches TO authenticated;
GRANT INSERT (hotel_id, price_type) ON TABLE rate_import_batches TO authenticated;
GRANT UPDATE (status, period_end_inclusive, period_years_confirmed)
ON TABLE rate_import_batches TO authenticated;

CREATE POLICY rate_import_batches_select_for_admin ON rate_import_batches
FOR SELECT TO authenticated
USING (current_app_role() = 'admin');

CREATE POLICY rate_import_batches_insert_for_admin ON rate_import_batches
FOR INSERT TO authenticated
WITH CHECK (current_app_role() = 'admin');

-- The dashboard may edit a draft, return a validated batch to draft, reject,
-- and disable. It may not validate or approve: those statuses are not in
-- WITH CHECK, so no dashboard write can produce them.
CREATE POLICY rate_import_batches_update_for_admin ON rate_import_batches
FOR UPDATE TO authenticated
USING (current_app_role() = 'admin')
WITH CHECK (current_app_role() = 'admin' AND status IN ('draft', 'rejected', 'disabled'));

GRANT SELECT ON TABLE rate_import_rows TO authenticated;
GRANT INSERT (
    batch_id,
    hotel_id,
    room_type_id,
    period_start,
    period_end,
    weekday_price_halalas,
    weekend_price_halalas,
    is_closed
) ON TABLE rate_import_rows TO authenticated;
GRANT UPDATE (
    room_type_id,
    period_start,
    period_end,
    weekday_price_halalas,
    weekend_price_halalas,
    is_closed,
    is_excluded
) ON TABLE rate_import_rows TO authenticated;

CREATE POLICY rate_import_rows_select_for_admin ON rate_import_rows
FOR SELECT TO authenticated
USING (current_app_role() = 'admin');

CREATE POLICY rate_import_rows_insert_for_admin ON rate_import_rows
FOR INSERT TO authenticated
WITH CHECK (current_app_role() = 'admin');

CREATE POLICY rate_import_rows_update_for_admin ON rate_import_rows
FOR UPDATE TO authenticated
USING (current_app_role() = 'admin')
WITH CHECK (current_app_role() = 'admin');

-- Read-only for the dashboard: nights are written by the approval (PR 2).
GRANT SELECT ON TABLE rate_import_nights TO authenticated;

CREATE POLICY rate_import_nights_select_for_admin ON rate_import_nights
FOR SELECT TO authenticated
USING (current_app_role() = 'admin');

-- Agent (hotel_agent, 0027): what pricing will read to find a night's base
-- price -- which batches are approved and in what order, and the nights.
-- Column-scoped: never who created, validated or approved a batch. No
-- write, and nothing on rate_import_rows.
GRANT SELECT (id, hotel_id, status, approval_seq) ON TABLE rate_import_batches TO hotel_agent;
GRANT SELECT (batch_id, hotel_id, room_type_id, stay_date, sell_price_halalas)
ON TABLE rate_import_nights TO hotel_agent;

CREATE POLICY rate_import_batches_agent_select ON rate_import_batches
FOR SELECT TO hotel_agent
USING (TRUE);

CREATE POLICY rate_import_nights_agent_select ON rate_import_nights
FOR SELECT TO hotel_agent
USING (TRUE);

-- A batch's status, its review choices and its rows' selling prices are
-- not cost or margin figures, so every admin may read their history:
-- widens the allow-list as 0035 left it by these pairs, restating it whole.
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
            ('allotments', 'total_rooms'),
            ('staff_replies', 'amounts'),
            ('rate_import_batches', 'status'),
            ('rate_import_batches', 'period_end_inclusive'),
            ('rate_import_batches', 'period_years_confirmed'),
            ('rate_import_rows', 'room_type_id'),
            ('rate_import_rows', 'period_start'),
            ('rate_import_rows', 'period_end'),
            ('rate_import_rows', 'weekday_price_halalas'),
            ('rate_import_rows', 'weekend_price_halalas'),
            ('rate_import_rows', 'is_closed'),
            ('rate_import_rows', 'is_excluded')
        )
    )
);
