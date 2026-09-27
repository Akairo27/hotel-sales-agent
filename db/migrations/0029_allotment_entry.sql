-- Allotment entry: the still-unbuilt screen migration 0016 deferred. Until
-- this migration nothing in the schema can ever CREATE an allotment or
-- room_night_inventory row except service_role or a test seed
-- (docs/plans/manual-entry.md section 7). This is that write path.
--
-- One SECURITY DEFINER function, admin_set_allotments (plan section D2),
-- not the UPDATE-then-INSERT-via-RLS-policy pattern every other admin
-- write here uses (0016, 0018, 0021) -- see ARCHITECTURE.md's security
-- section for why. Hardening, verified by a test, not by this comment: a
-- fixed NOLOGIN owner role scoped to exactly the columns its body uses;
-- SET search_path = '' with every object schema-qualified (pg_catalog is
-- always searched regardless, so generate_series/unnest/now/
-- current_setting/set_config need no prefix); EXECUTE revoked from PUBLIC
-- and anon, granted only to authenticated; the actor is read via the same
-- expression auth.uid() itself uses, not by calling it, so this role
-- needs no privilege on schema auth at all.

-- ---------------------------------------------------------------------------
-- The dedicated owner role.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'allotment_entry_writer') THEN
        CREATE ROLE allotment_entry_writer NOLOGIN NOINHERIT;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO allotment_entry_writer;

-- Reference reads: the function validates room_type_id genuinely belongs
-- to hotel_id (allotments has no composite FK tying the two, unlike
-- holds/quotes' FK against room_types(id, hotel_id)). Scoped to exactly
-- what the two EXISTS checks below read.
GRANT SELECT (id) ON TABLE hotels TO allotment_entry_writer;
GRANT SELECT (id, hotel_id) ON TABLE room_types TO allotment_entry_writer;

CREATE POLICY hotels_select_for_allotment_writer ON hotels
FOR SELECT TO allotment_entry_writer
USING (true);

CREATE POLICY room_types_select_for_allotment_writer ON room_types
FOR SELECT TO allotment_entry_writer
USING (true);

-- The owner role calls these two SECURITY DEFINER functions (0010) itself
-- to decide whether to allow the write at all -- it needs EXECUTE on them
-- exactly as authenticated does, since NOINHERIT means it gets nothing
-- from any other role's grants.
GRANT EXECUTE ON FUNCTION current_app_role() TO allotment_entry_writer;
GRANT EXECUTE ON FUNCTION current_user_can_view_cost() TO allotment_entry_writer;

-- allotments: full column set for SELECT (the function must read a
-- night's current total_rooms/cost_per_night to decide created vs.
-- updated vs. unchanged, and to name a night in an error), INSERT to
-- create a night's row, UPDATE for the two columns staff actually enter.
GRANT SELECT (id, hotel_id, room_type_id, stay_date, total_rooms, cost_per_night)
ON TABLE allotments TO allotment_entry_writer;
GRANT INSERT (hotel_id, room_type_id, stay_date, total_rooms, cost_per_night)
ON TABLE allotments TO allotment_entry_writer;
GRANT UPDATE (total_rooms, cost_per_night) ON TABLE allotments TO allotment_entry_writer;

CREATE POLICY allotments_select_for_allotment_writer ON allotments
FOR SELECT TO allotment_entry_writer
USING (true);

CREATE POLICY allotments_insert_for_allotment_writer ON allotments
FOR INSERT TO allotment_entry_writer
WITH CHECK (true);

CREATE POLICY allotments_update_for_allotment_writer ON allotments
FOR UPDATE TO allotment_entry_writer
USING (true)
WITH CHECK (true);

-- room_night_inventory: reserved/held are read-only here -- only
-- services/inventory's hold/booking flow sets them. Only `total` is
-- written, alongside its allotments row.
GRANT SELECT (allotment_id, stay_date, total, reserved, held)
ON TABLE room_night_inventory TO allotment_entry_writer;
GRANT INSERT (
    allotment_id, stay_date, total
) ON TABLE room_night_inventory TO allotment_entry_writer;
GRANT UPDATE (total) ON TABLE room_night_inventory TO allotment_entry_writer;

CREATE POLICY room_night_inventory_select_for_allotment_writer ON room_night_inventory
FOR SELECT TO allotment_entry_writer
USING (true);

CREATE POLICY room_night_inventory_insert_for_allotment_writer ON room_night_inventory
FOR INSERT TO allotment_entry_writer
WITH CHECK (true);

CREATE POLICY room_night_inventory_update_for_allotment_writer ON room_night_inventory
FOR UPDATE TO allotment_entry_writer
USING (true)
WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Audit (D6): total_rooms is now audited like cost_per_night (0016), both
-- covering INSERT too, not just UPDATE. CREATE OR REPLACE plus a DROP +
-- re-CREATE of the trigger that used it -- 0016's file stays untouched
-- (forward-only migrations); this one only replaces the objects it made.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION allotments_audit_trigger() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    actor uuid := current_actor_id();
    old_cost bigint;
    old_rooms integer;
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'app.actor_id must be set before writing allotments';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        old_cost := old.cost_per_night;
        old_rooms := old.total_rooms;
    END IF;
    IF new.cost_per_night IS DISTINCT FROM old_cost THEN
        INSERT INTO audit_log (table_name, row_id, column_name, old_value, new_value, changed_by)
        VALUES (
            'allotments', new.id::text, 'cost_per_night',
            to_jsonb(old_cost), to_jsonb(new.cost_per_night), actor
        );
    END IF;
    IF new.total_rooms IS DISTINCT FROM old_rooms THEN
        INSERT INTO audit_log (table_name, row_id, column_name, old_value, new_value, changed_by)
        VALUES (
            'allotments', new.id::text, 'total_rooms',
            to_jsonb(old_rooms), to_jsonb(new.total_rooms), actor
        );
    END IF;
    RETURN new;
END;
$$;

DROP TRIGGER allotments_audit_cost_per_night ON allotments;

CREATE TRIGGER allotments_audit_insert
AFTER INSERT ON allotments
FOR EACH ROW
EXECUTE FUNCTION allotments_audit_trigger();

CREATE TRIGGER allotments_audit_update
AFTER UPDATE ON allotments
FOR EACH ROW
WHEN (
    old.cost_per_night IS DISTINCT FROM new.cost_per_night
    OR old.total_rooms IS DISTINCT FROM new.total_rooms
)
EXECUTE FUNCTION allotments_audit_trigger();

-- Room-count audit rows are not cost and must stay visible to an admin
-- without cost visibility (D6); the full USING clause is repeated because
-- ALTER POLICY replaces it wholesale, not appends -- carrying forward
-- every entry 0019 AND 0022 already added (read from both files, not
-- assumed from 0019 alone: overwriting based on a stale baseline would
-- have silently dropped 0022's three price_overrides columns from the
-- allow-list).
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
            ('allotments', 'total_rooms')
        )
    )
);

-- D4: a night is a calendar date at the hotel; every hotel here is in
-- Saudi Arabia, one timezone. A separate, parameterized helper (not
-- now() inline) so the midnight boundary is testable with a controlled
-- instant, not the real clock. Not granted to authenticated: the RPC
-- always uses the real now().
CREATE FUNCTION allotment_entry_riyadh_date(p_instant timestamptz) RETURNS date
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT (p_instant AT TIME ZONE 'Asia/Riyadh')::date
$$;

REVOKE EXECUTE ON FUNCTION allotment_entry_riyadh_date(timestamptz) FROM public;
GRANT EXECUTE ON FUNCTION allotment_entry_riyadh_date(timestamptz) TO allotment_entry_writer;

-- ---------------------------------------------------------------------------
-- admin_set_allotments: creates or updates every night's allotments/
-- room_night_inventory row across [p_from_date, p_to_date_exclusive), all
-- at one total_rooms and cost_per_night.
--
-- Explicit per-night SELECT ... FOR UPDATE then INSERT or UPDATE, never
-- ON CONFLICT DO UPDATE: an explicit branch is what lets this report
-- created/updated/unchanged per night and name the first night a
-- reduction is refused on, neither of which a single bulk upsert can do.
-- The reduction check below is a named, friendly error; the real
-- authority is still room_night_inventory's own inventory_never_oversold
-- CHECK (CLAUDE.md rule 4), which fires on the UPDATE regardless.
--
-- dry_run performs every write inside a nested block, then raises a
-- private sentinel to unwind it -- not a conditional skip, because
-- skipping the writes would mean the CHECK constraints that are the real
-- enforcement never run during a preview, so a preview could report
-- success on a write the database would actually reject.
--
-- Returns one row per night, not three totals: the dry-run preview needs
-- to show *which* nights fall into each bucket, and a caller gets counts
-- by grouping this result (owner-approved). RETURNS TABLE columns are
-- prefixed out_ so they can never collide with a same-named table column
-- this function also reads (plpgsql's variable_conflict = error would
-- refuse to run otherwise).
CREATE FUNCTION admin_set_allotments(
    p_hotel_id bigint,
    p_room_type_id bigint,
    p_from_date date,
    p_to_date_exclusive date,
    p_total_rooms integer,
    p_cost_per_night bigint,
    p_dry_run boolean DEFAULT false
)
RETURNS TABLE (
    out_stay_date date,
    out_action text,
    out_total_rooms integer,
    out_cost_per_night bigint,
    out_reserved integer,
    out_held integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_actor uuid;
    v_riyadh_today date;
    v_nights integer;
    v_night date;
    v_allotment_id bigint;
    v_existing_total integer;
    v_existing_cost bigint;
    v_reserved integer;
    v_held integer;
    v_allotment_found boolean;
    v_inventory_found boolean;
    v_action text;
    v_dates date[] := '{}';
    v_actions text[] := '{}';
    v_reserved_arr integer[] := '{}';
    v_held_arr integer[] := '{}';
BEGIN
    IF public.current_app_role() IS DISTINCT FROM 'admin' OR NOT public.current_user_can_view_cost() THEN
        RAISE EXCEPTION 'not permitted to enter allotments' USING ERRCODE = '42501';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM public.hotels h WHERE h.id = p_hotel_id) THEN
        RAISE EXCEPTION 'hotel % does not exist', p_hotel_id;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.room_types rt WHERE rt.id = p_room_type_id AND rt.hotel_id = p_hotel_id
    ) THEN
        RAISE EXCEPTION 'room type % does not belong to hotel %', p_room_type_id, p_hotel_id;
    END IF;

    IF p_from_date >= p_to_date_exclusive THEN
        RAISE EXCEPTION 'from_date must be before to_date_exclusive';
    END IF;
    v_nights := p_to_date_exclusive - p_from_date;
    -- 366, not 180 (admin_upsert_price_overrides' cap): a hotel entering a
    -- full calendar year of rooms and cost at once, including a leap day,
    -- is the expected common case here, unlike a short-lived price
    -- override. A two-years-at-once typo (over 700 nights) still trips
    -- this. Named MAX_ALLOTMENT_ENTRY_RANGE_NIGHTS in the admin code.
    IF v_nights > 366 THEN
        RAISE EXCEPTION 'date range must not exceed 366 nights';
    END IF;

    v_riyadh_today := public.allotment_entry_riyadh_date(now());
    IF p_from_date < v_riyadh_today THEN
        RAISE EXCEPTION 'from_date must not be before today (%) in Asia/Riyadh', v_riyadh_today;
    END IF;

    IF p_total_rooms < 0 THEN
        RAISE EXCEPTION 'total_rooms must not be negative';
    END IF;
    IF p_cost_per_night < 0 THEN
        RAISE EXCEPTION 'cost_per_night must not be negative';
    END IF;

    -- Same expression the live auth.uid() evaluates to on
    -- hotel-sales-agent-dev (read from pg_proc, not assumed): it tries the
    -- flat GUC first, then falls back to the JSON claims blob newer
    -- PostgREST versions set instead -- deliberately not a call to
    -- auth.uid() itself, so this role needs no privilege on schema auth.
    v_actor := COALESCE(
        NULLIF(current_setting('request.jwt.claim.sub', true), ''),
        NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub'
    )::uuid;
    IF v_actor IS NULL THEN
        RAISE EXCEPTION 'not permitted to enter allotments' USING ERRCODE = '42501';
    END IF;
    PERFORM set_config('app.actor_id', v_actor::text, true);

    -- A nested block purely so dry_run can unwind every write below via
    -- its own implicit savepoint while keeping the per-night results
    -- already collected into the plpgsql arrays above -- those are
    -- interpreter-local values, not database state, so they survive the
    -- rollback this block's sentinel triggers.
    BEGIN
        FOR v_night IN
            SELECT generate_series(p_from_date, p_to_date_exclusive - 1, interval '1 day')::date
        LOOP
            SELECT a.id, a.total_rooms, a.cost_per_night
            INTO v_allotment_id, v_existing_total, v_existing_cost
            FROM public.allotments a
            WHERE a.hotel_id = p_hotel_id AND a.room_type_id = p_room_type_id AND a.stay_date = v_night
            FOR UPDATE;
            v_allotment_found := FOUND;
            v_inventory_found := false;
            v_reserved := 0;
            v_held := 0;

            IF v_allotment_found THEN
                SELECT rni.reserved, rni.held INTO v_reserved, v_held
                FROM public.room_night_inventory rni
                WHERE rni.allotment_id = v_allotment_id AND rni.stay_date = v_night
                FOR UPDATE;
                v_inventory_found := FOUND;
                IF NOT v_inventory_found THEN
                    v_reserved := 0;
                    v_held := 0;
                END IF;
            END IF;

            IF p_total_rooms < v_reserved + v_held THEN
                RAISE EXCEPTION
                    'cannot reduce rooms below already reserved or held for night % '
                    '(reserved % + held % exceeds requested %)',
                    v_night, v_reserved, v_held, p_total_rooms;
            END IF;

            IF v_allotment_found AND v_inventory_found
                AND v_existing_total = p_total_rooms AND v_existing_cost = p_cost_per_night
            THEN
                v_action := 'unchanged';
            ELSIF v_allotment_found THEN
                UPDATE public.allotments a
                SET total_rooms = p_total_rooms, cost_per_night = p_cost_per_night
                WHERE a.id = v_allotment_id;
                -- A room_night_inventory row can be missing even though its
                -- allotments row exists: the trial database already holds
                -- exactly this orphan (docs/plans/manual-entry.md section 7
                -- -- one allotment, zero inventory rows), from before this
                -- function was the only writer of either table. Repairing it
                -- here, rather than raising, is what lets an admin close that
                -- gap simply by re-entering the night through this same
                -- screen.
                IF v_inventory_found THEN
                    UPDATE public.room_night_inventory rni
                    SET total = p_total_rooms
                    WHERE rni.allotment_id = v_allotment_id AND rni.stay_date = v_night;
                ELSE
                    INSERT INTO public.room_night_inventory (allotment_id, stay_date, total)
                    VALUES (v_allotment_id, v_night, p_total_rooms);
                END IF;
                v_action := 'updated';
            ELSE
                INSERT INTO public.allotments (hotel_id, room_type_id, stay_date, total_rooms, cost_per_night)
                VALUES (p_hotel_id, p_room_type_id, v_night, p_total_rooms, p_cost_per_night)
                RETURNING id INTO v_allotment_id;
                INSERT INTO public.room_night_inventory (allotment_id, stay_date, total)
                VALUES (v_allotment_id, v_night, p_total_rooms);
                v_action := 'created';
            END IF;

            v_dates := v_dates || v_night;
            v_actions := v_actions || v_action;
            v_reserved_arr := v_reserved_arr || v_reserved;
            v_held_arr := v_held_arr || v_held;
        END LOOP;

        IF p_dry_run THEN
            -- ZZ001: a private sentinel, not a real Postgres or
            -- application condition -- chosen so it can never collide
            -- with a genuine error from the loop above (a real error
            -- there propagates past this handler untouched, since it
            -- only matches this exact SQLSTATE).
            RAISE EXCEPTION USING ERRCODE = 'ZZ001', MESSAGE = 'admin_set_allotments: discarding dry run writes';
        END IF;
    EXCEPTION WHEN SQLSTATE 'ZZ001' THEN
        NULL;
    END;

    -- unnest() with more than one argument cannot carry a column
    -- definition list (confirmed against real Postgres, not assumed) --
    -- the fix is parallel SRFs directly in the target list instead of a
    -- single multi-argument unnest() in the FROM clause: Postgres runs
    -- several set-returning functions in the same SELECT list in
    -- lockstep, which is exactly the zip these four equal-length arrays
    -- need.
    RETURN QUERY
    SELECT
        unnest(v_dates), unnest(v_actions), p_total_rooms, p_cost_per_night,
        unnest(v_reserved_arr), unnest(v_held_arr);
END;
$$;

-- ALTER ... OWNER TO needs membership in the target role, which CREATE
-- ROLE alone does not grant. postgres on hotel-sales-agent-dev is
-- CREATEROLE but not a real superuser (confirmed live, not assumed --
-- 0016 notes the same about rolbypassrls), so it needs this bracket; a
-- real superuser (CI's local Postgres) already could, masking the gap
-- there. current_user, not a literal name. Kept until after the ACL
-- changes below too: once ownership moves, only the new owner (or an
-- inherited membership in it) can REVOKE/GRANT on the function.
GRANT allotment_entry_writer TO current_user;
ALTER FUNCTION admin_set_allotments(bigint, bigint, date, date, integer, bigint, boolean)
OWNER TO allotment_entry_writer;

REVOKE EXECUTE ON FUNCTION admin_set_allotments(
    bigint, bigint, date, date, integer, bigint, boolean
) FROM public;
REVOKE EXECUTE ON FUNCTION admin_set_allotments(
    bigint, bigint, date, date, integer, bigint, boolean
) FROM anon;
GRANT EXECUTE ON FUNCTION admin_set_allotments(
    bigint, bigint, date, date, integer, bigint, boolean
) TO authenticated;

REVOKE allotment_entry_writer FROM current_user;

-- ---------------------------------------------------------------------------
-- The dashboard's read side for booked/held counts. No cost column, same
-- masking-by-absence reasoning as price_overrides (0021): nothing here to
-- hide, so the grant is unconditional and the gate is the usual
-- row-visibility one every other *_for_dashboard view uses.
-- ---------------------------------------------------------------------------

CREATE VIEW room_night_availability_for_dashboard AS
SELECT
    allotment_id,
    stay_date,
    total,
    reserved,
    held
FROM room_night_inventory
WHERE current_app_role() IS NOT null;

GRANT SELECT ON room_night_availability_for_dashboard TO authenticated;
