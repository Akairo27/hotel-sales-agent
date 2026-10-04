-- Price-sheet import, PR 1: the schema (docs/price-import-plan-draft.md,
-- ARCHITECTURE.md §4 "عقود الفنادق وأوراق الأسعار", owner decisions
-- 2026-10-04). Nothing reads these tables yet: pricing starts reading
-- rate_import_nights in PR 2, and the admin screen arrives in PR 3.
--
-- Three tables:
--   rate_import_batches  one sheet's worth of prices for one hotel, and its
--                        review state.
--   rate_import_rows     the period rows as entered or extracted. Editable
--                        only while the batch is a draft; never deleted (a
--                        row is left out with is_excluded).
--   rate_import_nights   one base selling price per (batch, room type,
--                        night). Append-only, and the only one of the three
--                        pricing will read.
--
-- A sheet price is a base SELLING price, not a cost (client answer
-- 2026-09-24), so none of these columns reverse-derives cost and nothing
-- here sits behind current_user_can_view_cost(), as price_overrides (0021).
--
-- Which price wins a night: the approved batch with the highest
-- approval_seq that has that night. Disabling a batch is the whole undo: its
-- nights stop counting and the previous batch's show through, so nothing is
-- ever restored or deleted.
--
-- Status: draft -> validated -> approved <-> disabled, and draft or
-- validated -> rejected. The guard trigger below enforces that graph for
-- every role and stamps who and when itself. This migration gives the
-- dashboard NO path to 'validated' or 'approved': validation and approval
-- need the pricing code's own season and price-rule resolution (CLAUDE.md
-- rule 6 keeps Hijri conversion in lib/hijri.py), so they are built in
-- Python with PR 2, which also takes the per-hotel lock that makes
-- approval_seq order the same as commit order within a hotel.
--
-- This migration creates the tables, closes them to every role and adds
-- the guards. Migration 0038 adds the audit trail, the grants and the
-- policies. No write function in either: the dashboard's write path comes
-- with the screen (PR 3).

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

-- Standalone, not an identity column: the number is drawn at approval, not
-- at insert. A sequence never hands the same value out twice, so two
-- approvals can never tie. The guard is the only caller.
CREATE SEQUENCE rate_import_approval_seq AS bigint;
REVOKE ALL ON SEQUENCE rate_import_approval_seq FROM anon, authenticated, service_role;

CREATE TABLE rate_import_batches (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    hotel_id bigint NOT NULL REFERENCES hotels (id),
    -- Stated on every batch, never assumed (ARCHITECTURE.md §4 decision 2).
    -- Only selling prices are importable: cost is entered by staff.
    price_type text NOT NULL
    CONSTRAINT rate_import_batches_price_type_valid CHECK (price_type = 'sell'),
    status text NOT NULL DEFAULT 'draft'
    CONSTRAINT rate_import_batches_status_valid
    CHECK (status IN ('draft', 'validated', 'approved', 'disabled', 'rejected')),
    -- The reviewer's two per-batch choices (decisions 8 and 9). NULL and
    -- false mean "not decided yet"; a batch cannot be validated until both
    -- are settled.
    period_end_inclusive boolean,
    period_years_confirmed boolean NOT NULL DEFAULT FALSE,
    -- The sha256 of the rows as they were validated: approval refuses a
    -- batch whose rows no longer match it.
    validated_fingerprint text
    CONSTRAINT rate_import_batches_fingerprint_is_sha256
    CHECK (validated_fingerprint ~ '^[0-9a-f]{64}$'),
    -- No ON DELETE on any of these, as audit_log.changed_by (0011): a staff
    -- member who appears in a batch's history cannot be deleted from under it.
    created_by uuid NOT NULL REFERENCES app_users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    validated_by uuid REFERENCES app_users (id),
    validated_at timestamptz,
    approved_by uuid REFERENCES app_users (id),
    approved_at timestamptz,
    approval_seq bigint UNIQUE,
    disabled_by uuid REFERENCES app_users (id),
    disabled_at timestamptz,
    rejected_by uuid REFERENCES app_users (id),
    rejected_at timestamptz,
    -- Lets rows and nights carry hotel_id and prove it is the batch's own.
    UNIQUE (id, hotel_id),
    -- Each status names exactly the fields it must and must not carry. Every
    -- IS NOT NULL is explicit: a NULL inside a comparison would make the
    -- whole CHECK NULL, which a CHECK accepts (the hole 0034 left open).
    CONSTRAINT rate_import_batches_validation_complete CHECK (
        (
            status IN ('draft', 'rejected')
            AND validated_fingerprint IS NULL
            AND validated_by IS NULL
            AND validated_at IS NULL
        )
        OR (
            status IN ('validated', 'approved', 'disabled', 'rejected')
            AND validated_fingerprint IS NOT NULL
            AND validated_by IS NOT NULL
            AND validated_at IS NOT NULL
            AND period_end_inclusive IS NOT NULL
            AND period_years_confirmed
        )
    ),
    CONSTRAINT rate_import_batches_approval_complete CHECK (
        (
            status IN ('draft', 'validated', 'rejected')
            AND approved_by IS NULL
            AND approved_at IS NULL
            AND approval_seq IS NULL
        )
        OR (
            status IN ('approved', 'disabled')
            AND approved_by IS NOT NULL
            AND approved_at IS NOT NULL
            AND approval_seq IS NOT NULL
        )
    ),
    CONSTRAINT rate_import_batches_disable_complete CHECK (
        (status <> 'disabled' AND disabled_by IS NULL AND disabled_at IS NULL)
        OR (status = 'disabled' AND disabled_by IS NOT NULL AND disabled_at IS NOT NULL)
    ),
    CONSTRAINT rate_import_batches_rejection_complete CHECK (
        (status <> 'rejected' AND rejected_by IS NULL AND rejected_at IS NULL)
        OR (status = 'rejected' AND rejected_by IS NOT NULL AND rejected_at IS NOT NULL)
    )
);

CREATE TABLE rate_import_rows (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_id bigint NOT NULL,
    hotel_id bigint NOT NULL,
    room_type_id bigint NOT NULL,
    period_start date NOT NULL,
    -- Whether this day is itself a priced night is the batch's
    -- period_end_inclusive choice, so a one-day period is legal here.
    period_end date NOT NULL,
    -- Integer halalas (CLAUDE.md rule 5). A weekend night is one of the
    -- hotel's own weekend_days (0028).
    weekday_price_halalas bigint,
    weekend_price_halalas bigint,
    -- A closed period (decision 10) carries no price and produces no night.
    is_closed boolean NOT NULL DEFAULT FALSE,
    -- The reviewer leaves a row out instead of deleting it.
    is_excluded boolean NOT NULL DEFAULT FALSE,
    created_by uuid NOT NULL REFERENCES app_users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (batch_id, hotel_id) REFERENCES rate_import_batches (id, hotel_id),
    -- A row can never name a room type from another hotel (as 0007).
    FOREIGN KEY (room_type_id, hotel_id) REFERENCES room_types (id, hotel_id),
    CONSTRAINT rate_import_rows_period_in_order CHECK (period_end >= period_start),
    CONSTRAINT rate_import_rows_priced_or_closed CHECK (
        (is_closed AND weekday_price_halalas IS NULL AND weekend_price_halalas IS NULL)
        OR (
            NOT is_closed
            AND weekday_price_halalas IS NOT NULL
            AND weekend_price_halalas IS NOT NULL
            AND weekday_price_halalas > 0
            AND weekend_price_halalas > 0
        )
    )
);

CREATE INDEX rate_import_rows_batch_id ON rate_import_rows (batch_id);

CREATE TABLE rate_import_nights (
    batch_id bigint NOT NULL,
    hotel_id bigint NOT NULL,
    room_type_id bigint NOT NULL,
    stay_date date NOT NULL,
    sell_price_halalas bigint NOT NULL
    CONSTRAINT rate_import_nights_price_positive CHECK (sell_price_halalas > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    -- One price per batch, room type and night: never two to choose between.
    PRIMARY KEY (batch_id, room_type_id, stay_date),
    FOREIGN KEY (batch_id, hotel_id) REFERENCES rate_import_batches (id, hotel_id),
    FOREIGN KEY (room_type_id, hotel_id) REFERENCES room_types (id, hotel_id)
);

-- Pricing's lookup: every batch's price for one hotel, room type and night.
CREATE INDEX rate_import_nights_stay ON rate_import_nights (hotel_id, room_type_id, stay_date);

-- Deny by default from the moment the tables exist: RLS enabled and forced,
-- and everything revoked -- service_role included, since Supabase's default
-- ACL would otherwise leave it every privilege (0013). Until migration 0038
-- grants each role what it needs, only the owner reaches these tables.
ALTER TABLE rate_import_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE rate_import_batches FORCE ROW LEVEL SECURITY;
ALTER TABLE rate_import_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE rate_import_rows FORCE ROW LEVEL SECURITY;
ALTER TABLE rate_import_nights ENABLE ROW LEVEL SECURITY;
ALTER TABLE rate_import_nights FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE rate_import_batches FROM anon, authenticated, service_role;
REVOKE ALL ON TABLE rate_import_rows FROM anon, authenticated, service_role;
REVOKE ALL ON TABLE rate_import_nights FROM anon, authenticated, service_role;

-- ---------------------------------------------------------------------------
-- Guards. SECURITY DEFINER with a fixed search_path, as the audit triggers
-- (0016): they read app_users, the batch a row belongs to and the approval
-- sequence whatever the writing role may itself read, so the rules hold for
-- every role and every path, not only for the dashboard's.
-- ---------------------------------------------------------------------------

-- Who is acting. A dashboard request carries its user in the JWT, which the
-- caller cannot change; a backend process has no JWT and names the staff
-- member it acts for in app.actor_id (current_actor_id, 0016).
CREATE FUNCTION rate_import_actor_id() RETURNS uuid
LANGUAGE sql
STABLE
SET search_path = public
AS $$
    SELECT COALESCE(auth.uid(), current_actor_id())
$$;

-- Approval is for an active admin who can see cost (owner decision
-- 2026-10-04): the check that blocks a price below the floor is only
-- meaningful to someone who may see what the floor is made of.
CREATE FUNCTION rate_import_actor_may_approve(actor uuid) RETURNS boolean
LANGUAGE sql
STABLE
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT FROM app_users AS u
        WHERE u.id = actor AND u.is_active AND u.app_role = 'admin' AND u.can_view_cost
    )
$$;

CREATE FUNCTION rate_import_batch_has_nights(target_batch_id bigint) RETURNS boolean
LANGUAGE sql
STABLE
SET search_path = public
AS $$
    SELECT EXISTS (SELECT FROM rate_import_nights AS n WHERE n.batch_id = target_batch_id)
$$;

-- The effects of one status change on the stamp columns. Every stamp starts
-- from its old value (see rate_import_batches_guard), so a transition only
-- names what it changes.
CREATE FUNCTION rate_import_batches_apply_transition(
    old_batch rate_import_batches, new_batch rate_import_batches, actor uuid
) RETURNS rate_import_batches
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
    transition text := old_batch.status || '>' || new_batch.status;
    result rate_import_batches := new_batch;
BEGIN
    IF transition IN ('validated>approved', 'disabled>approved')
        AND NOT rate_import_actor_may_approve(actor) THEN
        RAISE EXCEPTION 'only an active admin who can view cost approves a rate import batch'
        USING ERRCODE = '42501';
    END IF;
    -- Nights are written just before approval; a batch that has them can
    -- only go on to be approved, so no night outlives a withdrawn batch.
    IF transition IN ('validated>draft', 'validated>rejected')
        AND rate_import_batch_has_nights(old_batch.id) THEN
        RAISE EXCEPTION 'a rate import batch that has nights can only be approved';
    END IF;

    CASE transition
        WHEN 'draft>draft' THEN
            NULL;
        WHEN 'draft>validated' THEN
            result.validated_by := actor;
            result.validated_at := now();
        WHEN 'validated>draft' THEN
            result.validated_fingerprint := NULL;
            result.validated_by := NULL;
            result.validated_at := NULL;
        WHEN 'validated>approved' THEN
            result.approved_by := actor;
            result.approved_at := now();
            result.approval_seq := nextval('rate_import_approval_seq');
        WHEN 'approved>disabled' THEN
            result.disabled_by := actor;
            result.disabled_at := now();
        -- Re-enabling keeps the original approval_seq: the batch returns to
        -- the place it held, it does not jump ahead of later approvals.
        WHEN 'disabled>approved' THEN
            result.disabled_by := NULL;
            result.disabled_at := NULL;
        WHEN 'draft>rejected', 'validated>rejected' THEN
            result.rejected_by := actor;
            result.rejected_at := now();
        ELSE
            RAISE EXCEPTION 'a rate import batch cannot go from % to %',
                old_batch.status, new_batch.status;
    END CASE;
    RETURN result;
END;
$$;

CREATE FUNCTION rate_import_batches_guard() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    actor uuid := rate_import_actor_id();
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'the acting user must be known before writing rate_import_batches';
    END IF;

    IF TG_OP = 'INSERT' THEN
        IF new.status <> 'draft' THEN
            RAISE EXCEPTION 'a rate import batch starts as a draft';
        END IF;
        new.created_by := actor;
        new.created_at := now();
        RETURN new;
    END IF;

    IF new.hotel_id <> old.hotel_id OR new.price_type <> old.price_type THEN
        RAISE EXCEPTION 'a rate import batch cannot change its hotel or price type';
    END IF;
    IF new.status = old.status AND old.status <> 'draft' THEN
        RAISE EXCEPTION 'only a draft rate import batch can be edited';
    END IF;
    IF new.status <> 'draft' AND (
        new.period_end_inclusive IS DISTINCT FROM old.period_end_inclusive
        OR new.period_years_confirmed <> old.period_years_confirmed
    ) THEN
        RAISE EXCEPTION 'the review choices of a rate import batch change only in a draft';
    END IF;

    -- The stamps are the guard's to write, never the caller's: each starts
    -- from its stored value, and only the transition changes it. The one
    -- value a caller supplies is the fingerprint, on draft -> validated.
    new.created_by := old.created_by;
    new.created_at := old.created_at;
    IF NOT (old.status = 'draft' AND new.status = 'validated') THEN
        new.validated_fingerprint := old.validated_fingerprint;
    END IF;
    new.validated_by := old.validated_by;
    new.validated_at := old.validated_at;
    new.approved_by := old.approved_by;
    new.approved_at := old.approved_at;
    new.approval_seq := old.approval_seq;
    new.disabled_by := old.disabled_by;
    new.disabled_at := old.disabled_at;
    new.rejected_by := old.rejected_by;
    new.rejected_at := old.rejected_at;

    new := rate_import_batches_apply_transition(old, new, actor);
    RETURN new;
END;
$$;

CREATE TRIGGER rate_import_batches_guard
BEFORE INSERT OR UPDATE ON rate_import_batches
FOR EACH ROW
EXECUTE FUNCTION rate_import_batches_guard();

-- Rows change only while their batch is a draft, so what was validated is
-- what gets approved. FOR SHARE makes a concurrent status change wait for
-- this write (and this write see the new status), not slip past it.
CREATE FUNCTION rate_import_rows_guard() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    actor uuid := rate_import_actor_id();
    batch_status text;
BEGIN
    IF actor IS NULL THEN
        RAISE EXCEPTION 'the acting user must be known before writing rate_import_rows';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF new.batch_id <> old.batch_id OR new.hotel_id <> old.hotel_id THEN
            RAISE EXCEPTION 'a rate import row cannot move to another batch or hotel';
        END IF;
        new.created_by := old.created_by;
        new.created_at := old.created_at;
    ELSE
        new.created_by := actor;
        new.created_at := now();
    END IF;

    SELECT b.status INTO batch_status
    FROM rate_import_batches AS b
    WHERE b.id = new.batch_id
    FOR SHARE;

    IF batch_status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'rate import rows change only while their batch is a draft';
    END IF;
    RETURN new;
END;
$$;

CREATE TRIGGER rate_import_rows_guard
BEFORE INSERT OR UPDATE ON rate_import_rows
FOR EACH ROW
EXECUTE FUNCTION rate_import_rows_guard();

-- Nights are written once: while the batch is validated, in the transaction
-- that then approves it. After approval no night can be added, and no role
-- is ever granted UPDATE or DELETE on the table (0038).
CREATE FUNCTION rate_import_nights_guard() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    batch_status text;
BEGIN
    SELECT b.status INTO batch_status
    FROM rate_import_batches AS b
    WHERE b.id = new.batch_id
    FOR SHARE;

    IF batch_status IS DISTINCT FROM 'validated' THEN
        RAISE EXCEPTION 'rate import nights are written only while their batch is validated';
    END IF;
    RETURN new;
END;
$$;

CREATE TRIGGER rate_import_nights_guard
BEFORE INSERT ON rate_import_nights
FOR EACH ROW
EXECUTE FUNCTION rate_import_nights_guard();
