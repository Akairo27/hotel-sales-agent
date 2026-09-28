# Plan: manual rate and room entry

**Status (2026-09-27): approved by the owner with the decisions in section 1. Two PRs: PR-1 (hotel location fields, migration 0028) is written and open as PR #63, not merged and not applied; PR-2 (allotment entry, migration 0029) follows.** Nothing in this plan is applied to any database. This file contains no secrets.

**The gates still apply and are not relaxed by the approval:** every migration is shown to the owner in full before it is applied, and is applied to `hotel-sales-agent-dev` only on the owner's explicit go-ahead at that moment; the exact audit and RLS change in PR-2 is shown as SQL before it is applied; nothing here restarts a service; there is no change in `pricing/`.

## 1. Decisions on record (owner, 2026-09-27)

- **D1. Scope of "rate".** Cost per night on the allotment, entered by staff. **Cost per night is required to create an allotment in this PR** (the column stays `NOT NULL`). A nullable cost, a selling price and `sell_adjustment_bps` come later, with the pricing work and the client's answers.
- **D2. One `SECURITY DEFINER` function, `admin_set_allotments` (option A).** Required hardening:
  - `SET search_path` to an empty value (or `pg_catalog` only), with **every object schema-qualified** in the function body (`public.allotments`, `public.current_app_role()` and so on).
  - A **fixed, non-login owner**: a dedicated role created in the migration (`NOLOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, `NOCREATEROLE`, `NOCREATEDB`, no memberships), using the idempotent `DO` block of migration 0027. The function is `ALTER FUNCTION ... OWNER TO` that role. Design direction, to be shown as SQL in PR-2: the role holds only the column-level privileges the function needs on `allotments`, `room_night_inventory` and the reference reads on `hotels` and `room_types`, and explicit `USING (true)` policies `TO` that role, each write policy paired with a `FOR SELECT` (CLAUDE.md rule 11), so row-level security stays on for everything (rule 10) and the privileges, not a `BYPASSRLS` attribute, do the narrowing.
  - `REVOKE EXECUTE ... FROM PUBLIC` and from `anon`; `GRANT EXECUTE` **only to `authenticated`** (the owner keeps it as owner). No other role.
  - **A test proves the `search_path` setting** (from `pg_proc.proconfig`), and a second test proves it behaviourally (a same-named decoy object placed where the caller's own `search_path` would find it is ignored).
  - **The deliberate departure from migration 0016's `SECURITY INVOKER` stance is documented in `ARCHITECTURE.md`**, with the reason: one audited, locked path that writes `allotments`, `room_night_inventory` and the audit rows together, with no new table privilege for `authenticated` and so no new policy to pair.
- **D3. Who may write.** An admin with `can_view_cost`, for every write. **Note for later:** if staff without cost visibility ever need to change room counts, that is a **separate definer function that can only touch rooms**, not a second RLS policy (permissive policies are OR-ed and row-level, so a second policy would also open `cost_per_night` through the column grant).
- **D4. Past nights are computed in `Asia/Riyadh`, not UTC.** A night is a calendar date at the hotel; today's date is `(now() AT TIME ZONE 'Asia/Riyadh')::date`, computed by an internal helper that takes the instant as a parameter and is **not** granted to `authenticated` (so the clock is testable without exposing a fake-now argument). A test covers the Riyadh midnight boundary (the second before and the second after 00:00 Riyadh, which is 21:00 UTC). The screen asks for the **last night, inclusive**, and converts it to the end-exclusive range the database uses. Timestamps everywhere else stay UTC (rule 6); `stay_date` is a plain date.
- **D5. Two PRs.** PR-1: hotel location fields, migration 0028 and the hotel form. PR-2: allotment entry, migration 0029.
- **D6. Audit room-count changes too, in PR-2**, with the same cost-visibility gating where it is relevant. Room counts are not cost, so their audit rows must be readable by an admin without cost visibility: the allow-list in `audit_log_select_admin_only` (migration 0019) gains the pair `('allotments', 'total_rooms')`. That is an `ALTER POLICY`, and **the exact audit and RLS change is shown to the owner as SQL before it is applied.** Cost rows keep the cost-visibility gate.

## 2. Scope

In: `hotels.city`, `weekend_days`, `zone`, `district_name` (approved 2026-09-24, recorded in `ARCHITECTURE.md` section 4); the hotel form fields; a room-count and cost-per-night entry screen.
Out: anything in `pricing/`; selling price, meal plans and FAREAST; rate-sheet import; bot changes (no new tool or parameter, and the agent does not read the new columns); the completeness list (unchanged until Plan A); deleting allotments (closing a night means setting its rooms to 0, which the availability check already sees as unavailable).

## 3. PR-1: migration 0028_hotel_location.sql

Additive only; no rename, no data rewrite beyond the column default.

- `city text`, CHECK `city IS NULL OR city IN ('makkah', 'madinah')`.
- `weekend_days smallint[] NOT NULL DEFAULT '{5,6}'` (ISO weekdays, Friday and Saturday), with a pure-expression CHECK (every element between 1 and 7, one dimension, at most seven elements). No custom validator function: a writer of a table whose CHECK calls a function needs EXECUTE on it.
- `zone text`, closed list `makkah_central`, `makkah_outside`, `madinah_central`, `madinah_north`, `madinah_west`, `madinah_south`, `madinah_outside`, plus a CHECK that ties the zone to its city and rejects a zone without a city.
- `district_name text`, CHECK `IS NULL OR length(btrim(district_name)) BETWEEN 1 AND 60`. An admin-entered proper noun, shown as is and never used as an instruction (the limited exception to "no free text" that the owner approved).
- `COMMENT ON COLUMN` for the four columns, and for `distance_to_haram_meters`: its reference point depends on the city (Masjid al-Haram for Makkah, Al-Masjid an-Nabawi for Madinah).
- No grant, no policy: `hotels` already grants `authenticated` table-level SELECT, INSERT and UPDATE with admin-only write policies (migration 0014). The backend roles have no privilege on `hotels`, so the agent and the worker cannot see the new columns.

Hotel form (`/hotels/[hotelId]`): a city select; a zone select (the seven zones grouped under their two cities, no client-side script; the server action and the database enforce the pairing); a district name field (at most 60 characters); seven weekend-day checkboxes (default Friday and Saturday). City, zone and district name are optional. **The form requires at least one weekend day, although the database allows an empty array** (a hotel with no weekend): an empty selection is far more likely to be a slip. Server-side validation mirrors the CHECKs, with a conformance test in the style of `hotelDetails.conformance.test.ts`. Built in PR #63.

## 4. PR-2: migration 0029_allotment_entry.sql

- The dedicated non-login owner role and its least-privilege grants and policies (D2).
- `admin_set_allotments(hotel_id, room_type_id, from_date, to_date_exclusive, total_rooms, cost_per_night, dry_run)`, `SECURITY DEFINER`, hardened as in D2, no dynamic SQL. It rejects with 42501 unless the caller is an admin with `can_view_cost`; validates the hotel and room-type pair, the range (start before end, a named maximum number of nights, no night before today in Riyadh) and non-negative integers; takes the actor from the JWT `sub` claim (the same expression `auth.uid()` uses, so the owner role needs nothing on schema `auth`) and sets `app.actor_id`; locks the existing nights (`SELECT ... FOR UPDATE`, ordered by date) so it serializes with holds; inserts or updates each night's `allotments` and `room_night_inventory` rows; refuses a reduction below `reserved + held` with a clean error naming the first date (the whole call rolls back); returns created, updated and unchanged counts. `dry_run` runs the same code and rolls back.
- Explicit insert or update, never `INSERT ... ON CONFLICT DO UPDATE` (the trap behind CLAUDE.md rule 11).
- Audit: an `AFTER INSERT` trigger for `cost_per_night` (only UPDATE is audited today), and audit of `total_rooms` changes (D6), plus the `ALTER POLICY` on `audit_log`.
- A view `room_night_availability_for_dashboard` (allotment id, total, reserved, held), gated like `allotments_for_dashboard`, so the screen can show booked counts. No cost in it.
- The internal Riyadh-date helper (D4); the maximum range as one named constant, mirrored in the admin code and checked by a conformance test.

Screen (extends `/allotments`; admin with cost visibility only): hotel, room type, first night, last night (inclusive), rooms, cost per night in halalas (integer, with an integer-only SAR display); a preview step that calls the same function with `dry_run` and shows the nights to create, update or leave, and any night that cannot be reduced because it is booked; the existing table gains booked and held columns, with inline edit through the same function for one night.

## 5. Grants by role

| Role | PR-1 (0028) | PR-2 (0029) |
|---|---|---|
| `authenticated` (staff) | Nothing new; the new `hotels` columns are covered by the existing table-level grants and admin-only write policies. | EXECUTE on `admin_set_allotments`; SELECT on `room_night_availability_for_dashboard`. No new table privilege. |
| the new non-login owner | not created yet | The column-level privileges and policies in D2, and nothing else. |
| `hotel_agent` | Nothing. No privilege on `hotels`, so the new columns are invisible to it. | Nothing. Its `allotments` grant is unchanged (no `allotments` column is added). The new view is classified "no privileges" in the manifest test. |
| `hotel_worker` | Nothing. | Nothing. |
| `anon` | Nothing. | Nothing (explicit REVOKE). |
| `service_role` | Default privileges as today. | Default privileges as today, checked by the default-privileges lockdown test. |

## 6. Tests

Postgres integration tests run in CI only (there is no Postgres on the trial host).

PR-1:
- **`hotels` constraints:** the city closed list; every valid city and zone pair passes, and a mismatched zone, or a zone with no city, fails; `weekend_days` default `{5,6}` for existing and new rows, values outside 1 to 7, an empty or NULL-element array and a multi-dimensional array behave as designed; `district_name` boundaries (blank, 1, 60 and 61 characters); the COMMENTs exist; no existing column or constraint changed.
- **Authorization:** an admin can set the new columns and a non-admin cannot (the existing policies cover them); the backend roles cannot read the new columns.
- **Admin (vitest, eslint, tsc strict):** the parse and validation of the new fields, the city and zone dependency, the weekend checkboxes mapping to `smallint[]`, a conformance test against the migration.

PR-2:
- **Hardening:** `proconfig` shows the empty `search_path`; a decoy object in the caller's `search_path` is ignored; the owner is `NOLOGIN` with the intended attributes and owns nothing else; the function's ACL is exactly the owner and `authenticated` (none for `PUBLIC`, `anon` or `service_role`); the owner's privileges equal an explicit manifest, as in `test_backend_roles.py`.
- **Authorization matrix:** an admin with cost visibility succeeds; an admin without it, a sales user, an inactive user and anon fail (42501 or no EXECUTE) and write nothing.
- **Riyadh boundary:** the helper returns the next day exactly at 21:00:00 UTC and the previous day one second earlier; a night on today's Riyadh date is accepted and yesterday's is rejected, with the expected dates computed inside the same transaction.
- **Behaviour:** a 3-night range creates exactly 3 allotments and 3 inventory rows with `total = total_rooms` and zero reserved and held, excluding the end date; re-running is idempotent; changing rooms updates both tables; reducing below `reserved + held` fails cleanly and changes nothing; every validation error; dry run equals the real plan and writes nothing.
- **Audit:** cost rows on insert and update, and room-count rows, each with the right actor; cost rows visible only to cost-visible admins, room-count rows visible to every admin.
- **Concurrency (real concurrent transactions):** reducing rooms while a hold takes the last room of the same night gives exactly one consistent outcome and never oversells; two concurrent entries over the same range serialize with no duplicates and no deadlock.
- **Backend roles and end to end:** `test_backend_roles.py` passes with the new view classified; nights entered through the function are sellable (availability and a quote as `hotel_agent` succeed) and no cost leaks.
- **Default privileges and ACL drift** for the new function, view and role.

## 7. Facts this plan rests on (verified in the repository, 2026-09-27)

- Nothing in non-test code creates `allotments` or `room_night_inventory` rows; only the test seeds do. The trial database holds 1 allotment (2026-09-19) and 0 inventory rows, so nothing is sellable yet.
- `allotments` has one row per (hotel, room type, night) with `total_rooms` and `cost_per_night NOT NULL`; `room_night_inventory` is keyed by (allotment, night) with total, reserved, held and the constraint `inventory_never_oversold`.
- Migration 0016 gave `authenticated` only `SELECT (id)` and `UPDATE (cost_per_night)` on `allotments`, no INSERT (its own comment says allotment creation belongs to the unbuilt entry screen), plus the masking view, an UPDATE audit trigger and `admin_set_allotment_cost`.
- Migration 0014: `hotels` and `room_types` have SELECT, INSERT and UPDATE for `authenticated`, admin-only write policies, and no DELETE.
- The pricing engine reads `cost_per_night` per night and the availability query joins inventory, so a night needs both rows to be sellable.

## 8. Backlog

- **Drop `admin_set_allotment_cost` (migration 0016) in a later migration.** The admin entry screen (PR #69) moved its only caller — the `/allotments` inline edit — to `admin_set_allotments`, so the function, its grants and its policies are now dead. Not dropped here: forward-only migrations mean removing it is its own reviewed migration, not a side effect of the PR that stopped calling it.
