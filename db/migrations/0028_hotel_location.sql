-- Hotel location profile: the city, the zone within it, an optional district
-- or street name, and which weekdays count as the hotel's weekend. Approved
-- by the owner on 2026-09-24 and recorded in ARCHITECTURE.md section 4; the
-- plan is docs/plans/manual-entry.md (PR-1 of two).
--
-- Additive only: four new columns, no rename, no change to an existing
-- column or constraint. Every value is entered by staff in the dashboard;
-- rate-sheet import never writes any of them, and nothing in the agent
-- reads them yet (the backend roles have no privilege on hotels at all, so
-- these columns are invisible to them).
--
-- No grant and no policy is added. Migration 0014 already grants
-- authenticated table-level SELECT, INSERT and UPDATE on hotels with
-- admin-only write policies, and a table-level grant covers a new column.
--
-- Presence is not enforced in SQL, as in migration 0023: the table already
-- holds rows, migrations are forward-only, and there is no honest default
-- for which city a hotel is in. Only weekend_days has a default, because
-- the owner decided one (Friday and Saturday). The completeness list the
-- agent will use later is unchanged.

ALTER TABLE hotels
ADD COLUMN city text
CONSTRAINT hotels_city_valid
CHECK (city IS NULL OR city IN ('makkah', 'madinah')),

-- ISO weekdays: 1 is Monday, 7 is Sunday, so 5 and 6 are Friday and
-- Saturday. A pure expression, deliberately no validator function: a role
-- that writes a table whose CHECK calls a function needs EXECUTE on it (the
-- lesson of migration 0027). Every element must be 1..7, a NULL element is
-- rejected explicitly rather than left to how `<@` treats NULL, an empty
-- array is allowed (a hotel with no weekend days), and a multi-dimensional
-- array is not.
ADD COLUMN weekend_days smallint[] NOT NULL DEFAULT '{5,6}'
CONSTRAINT hotels_weekend_days_valid
CHECK (
    weekend_days <@ ARRAY[1, 2, 3, 4, 5, 6, 7]::smallint[]
    AND array_position(weekend_days, NULL) IS NULL
    AND cardinality(weekend_days) <= 7
    AND (array_ndims(weekend_days) IS NULL OR array_ndims(weekend_days) = 1)
),

ADD COLUMN zone text
CONSTRAINT hotels_zone_valid
CHECK (
    zone IS NULL
    OR zone IN (
        'makkah_central',
        'makkah_outside',
        'madinah_central',
        'madinah_north',
        'madinah_west',
        'madinah_south',
        'madinah_outside'
    )
),

-- A short proper noun (a district or a street) entered by an admin, shown
-- as it is and never used as an instruction. A limited exception to the
-- "no free text" rule at the top of migration 0023, approved by the owner;
-- address_text is the precedent. The length bound is what keeps it a name.
ADD COLUMN district_name text
CONSTRAINT hotels_district_name_valid
CHECK (district_name IS NULL OR length(btrim(district_name)) BETWEEN 1 AND 60);

-- The zone must belong to the hotel's city: the zone names are prefixed by
-- their city, so the check compares that prefix. A zone without a city is
-- rejected, because a NULL city leaves nothing to match it against. Adding
-- a zone later is a one-line forward migration, which is the intended cost
-- (the same reasoning as the amenities list).
ALTER TABLE hotels
ADD CONSTRAINT hotels_zone_matches_city
CHECK (
    zone IS NULL
    OR (city IS NOT NULL AND left(zone, length(city) + 1) = city || '_')
);

COMMENT ON COLUMN hotels.city IS
'The hotel''s city: makkah or madinah. '
'Entered by staff; rate-sheet import never writes it.';

COMMENT ON COLUMN hotels.weekend_days IS
'ISO weekdays (1 Monday to 7 Sunday) that count as this hotel''s weekend. '
'Default {5,6}, Friday and Saturday. '
'Entered by staff; rate-sheet import never writes it.';

COMMENT ON COLUMN hotels.zone IS
'Location zone, prefixed by the hotel''s city (hotels_zone_matches_city). '
'Entered by staff; rate-sheet import never writes it.';

COMMENT ON COLUMN hotels.district_name IS
'District or street name: a short proper noun typed by an admin, '
'1 to 60 characters, shown as is and never used as an instruction. '
'Entered by staff; rate-sheet import never writes it.';

-- The reference point of the existing distance column depends on the city,
-- so distances are comparable only between hotels of the same city.
COMMENT ON COLUMN hotels.distance_to_haram_meters IS
'Metres from the city''s reference mosque: Masjid al-Haram for a hotel in makkah, '
'Al-Masjid an-Nabawi for a hotel in madinah. '
'Compare distances only within one city.';
