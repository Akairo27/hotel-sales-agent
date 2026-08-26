-- Hotel and room-type attributes the sales agent needs in order to answer
-- a customer at all. Migration 0001 created both tables with nothing but a
-- name, and ARCHITECTURE.md §4 listed them as the only tables with no
-- column list — hotel content was never designed, not designed-and-deferred.
--
-- Without these, ARCHITECTURE.md §7's `search_alternatives` tool ("فنادق
-- بديلة بنفس التواريخ") has no basis on which to pick an alternative, and
-- the first question a Makkah customer asks — how far is it from the Haram
-- — is unanswerable.
--
-- Every attribute here is structured (a number, a time, or a value from a
-- closed list). There is deliberately no free-text description column: any
-- prose stored here would enter the model's context, and the output guard
-- (ARCHITECTURE.md §7) validates monetary figures only — it cannot tell
-- that a sentence claims an amenity the hotel does not have.

ALTER TABLE hotels
ADD COLUMN distance_to_haram_meters integer
CONSTRAINT hotels_distance_to_haram_positive
CHECK (distance_to_haram_meters IS NULL OR distance_to_haram_meters > 0),

ADD COLUMN star_rating smallint
CONSTRAINT hotels_star_rating_valid
CHECK (star_rating IS NULL OR star_rating BETWEEN 1 AND 5),

ADD COLUMN address_text text
CONSTRAINT hotels_address_text_not_blank
CHECK (address_text IS NULL OR length(btrim(address_text)) > 0),

ADD COLUMN check_in_time time,
ADD COLUMN check_out_time time,

-- Migration 0014 grants DELETE on hotels to no role but service_role,
-- because allotments and holds reference them. Retiring a hotel therefore
-- has to be a flag, not a row deletion.
ADD COLUMN is_active boolean NOT NULL DEFAULT TRUE;

-- Each CHECK above spells out `IS NULL OR ...` rather than relying on a
-- bare comparison. A bare `CHECK (star_rating BETWEEN 1 AND 5)` evaluates
-- to NULL for a NULL input, and Postgres treats a NULL check result as
-- passing — the constraint would read as if it rejected NULL while
-- silently admitting it.
--
-- Presence is not enforced here. These columns are nullable because the
-- table already holds rows, migrations are forward-only, and there is no
-- honest default for "how far is this hotel from the Haram" — inventing
-- one would put a fabricated number in front of a customer. The admin
-- dashboard requires the fields on save; gating what the agent may offer
-- on a complete profile belongs to phase 4, when the agent first reads
-- these columns.

ALTER TABLE room_types
ADD COLUMN capacity_adults smallint
CONSTRAINT room_types_capacity_adults_valid
CHECK (capacity_adults IS NULL OR capacity_adults BETWEEN 1 AND 20),

ADD COLUMN size_sqm smallint
CONSTRAINT room_types_size_sqm_positive
CHECK (size_sqm IS NULL OR size_sqm > 0),

ADD COLUMN bed_configuration text
CONSTRAINT room_types_bed_configuration_valid
CHECK (
    bed_configuration IS NULL
    OR bed_configuration IN ('single', 'double', 'twin', 'triple', 'quad')
);

-- Amenities as rows against a closed list rather than a text column on
-- hotels: the list is what stops the agent from describing a facility that
-- does not exist, and a CHECK keeps that list under code review. Adding an
-- amenity is a one-line forward migration, which is the intended cost.
CREATE TABLE hotel_amenities (
    hotel_id bigint NOT NULL REFERENCES hotels (id),
    amenity text NOT NULL
    CONSTRAINT hotel_amenities_known_amenity CHECK (
        amenity IN (
            'haram_view',
            'shuttle_to_haram',
            'breakfast_included',
            'restaurant',
            'room_service',
            'prayer_room',
            'wifi',
            'parking',
            'laundry',
            'elevator',
            'wheelchair_accessible',
            'family_rooms'
        )
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (hotel_id, amenity)
);

ALTER TABLE hotel_amenities ENABLE ROW LEVEL SECURITY;
ALTER TABLE hotel_amenities FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE hotel_amenities FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE hotel_amenities TO service_role;

-- Read for any active app_users row, write for admins — the same split
-- migration 0014 applies to hotels itself. An amenity is added or removed,
-- never edited in place (the whole row is its key), so no UPDATE is
-- granted to authenticated.
GRANT SELECT, INSERT, DELETE ON TABLE hotel_amenities TO authenticated;

-- CLAUDE.md rule 11: the FOR SELECT policy is created in the same
-- migration as the write policies and covers the same rows. Without it the
-- INSERT's own row would be invisible to the statement and the write would
-- fail — confirmed the hard way in PRs D, E and F.
CREATE POLICY hotel_amenities_select_for_active_users ON hotel_amenities
FOR SELECT TO authenticated
USING (current_app_role() IS NOT NULL);

CREATE POLICY hotel_amenities_insert_for_admin ON hotel_amenities
FOR INSERT TO authenticated
WITH CHECK (current_app_role() = 'admin');

CREATE POLICY hotel_amenities_delete_for_admin ON hotel_amenities
FOR DELETE TO authenticated
USING (current_app_role() = 'admin');
