# Price sheet import: plan draft (2026-10-04)

Status: PR 1 (the schema, migrations 0037 and 0038) is built; everything else
is still a plan. Nothing here has been applied to any database.
Basis: ARCHITECTURE.md §4 "عقود الفنادق وأوراق الأسعار" as recorded
(2026-09-24/25), plus the owner's decisions of 2026-10-04. The file parser is
out of scope until real samples are reviewed.

## Basis

From the record:
- Sheet periods are not seasons. They become per-night price rows.
- ask = base × (10000 + sell_adjustment_bps) / 10000 × demand factor.
  `sell_adjustment_bps` is a new nullable column on `price_rules`, resolved
  through the existing inheritance chain (empty = no adjustment).
- Base and cost both present: min_allowed = cost + minimum profit, as today.
  An ask below it is the explicit `InconsistentPriceConfigurationError`, never
  a silent raise. Import blocks a base below cost.
- Base without cost: no drop below the base, min_allowed = ask, no negotiation.
  DEFERRED indefinitely (owner, 2026-10-04): the client says cost is complete
  in his system, so every sellable night has a cost. Kept on record, not built.
- FAREAST: room price + (meal price per person per night × guests × nights).
  The meal part is exempt from season and demand adjustments. Not bot-sellable
  until the hotel's meal price is entered. Nationality check in code.

Owner, 2026-10-04:
- Batches with human approval. Undo = disabling the batch, no DELETE.
- Each quote night records its price source. One stay may mix sources.
- Overrides keep bypassing the floor; selling below cost happens only there.
- An override sets the room component only; the FAREAST meal part is added
  on top.
- A package with no meal price stays unpriced and the agent escalates.
- The approver must hold `can_view_cost`. An uploader may approve their own
  batch for now.
- Prices are read from a separate per-night table. The latest approved batch
  wins a night; disabling it reveals the previous one.
- The client's real data is entered through the app's screens, never in
  migrations, code or tests.

## D. Schema

Sheet prices (BUILT in PR 1: migration 0037 creates the tables, closed to
every role, and the guards; 0038 adds audit, grants and policies. Nothing
reads them yet):
- `rate_import_batches`: hotel, `price_type` (sell), status, the reviewer's
  per-batch choices (period end inclusive or not, years confirmed), the
  fingerprint of the validated rows, and created / validated / approved /
  disabled / rejected by and at. Each status has explicit IS NOT NULL checks
  on its own fields.
- `rate_import_rows`: the period rows as entered or extracted: room type,
  period start and end, weekday price, weekend price, closed flag, who added
  it. Editable only while the batch is a draft. No DELETE: a row is excluded
  by a flag.
- `rate_import_nights`: one row per (batch, room type, stay date) with
  `sell_price_halalas` (bigint, above zero). Written once and never edited:
  nights are inserted while the batch is `validated`, in the transaction that
  then approves it; after approval none can be added, and a batch that has
  nights can only go on to be approved. This is the table pricing reads.
- Which price wins a night: the latest approved batch that has that night.
  "Latest" is decided by `approval_seq`, a monotonic sequence number the guard
  draws at approval, never by a timestamp. Disabling a batch reveals the
  previous one, so undo needs no restore step. Re-enabling keeps the batch's
  original `approval_seq`: it returns to the place it held.
- Every stamp (who and when, and `approval_seq`) is written by the guard, not
  the caller. The acting user is the dashboard's signed-in user, or for a
  backend process the staff member named in `app.actor_id`.
- Not created: `rate_import_applied` (no before/after restore is needed) and
  `rate_import_events` (status changes go to `audit_log`).
  `rate_import_files` comes with the parser.

Pricing columns (second migration, with the pricing PR):
- `price_rules.sell_adjustment_bps`, nullable integer, bounded above -10000.
  The dashboard view and `admin_upsert_price_rule` are restated to carry it.
- `quotes.nights` gains `price_source` (override / sheet / fallback). A sheet
  night records the base price, the batch id, the adjustment and its rule id,
  the price after adjustment, the demand fields, cost and minimum profit.
  The validator function behind `quotes_nights_are_complete` is replaced with
  a three-shape version; old rows without the key stay valid in the two old
  shapes. This changes a constraint and needs approval.

FAREAST (later migration):
- `meal_packages` (code, is_active) and `meal_package_nationalities`
  (package, ISO country code), editable by an admin. Nothing is seeded: the
  list starts empty, and a package with an empty list is open to nobody.
- `hotel_meal_prices`: hotel, package, meal price per person per night
  (halalas), `bot_sellable` default false, with a check that it cannot be
  true without a price.
- `quotes` gains nullable package, guests, meal price and meal total columns.
  `conversations` gains the claimed nationality (country code).

Access rules for every new table:
- RLS enabled and forced; all privileges revoked from `anon` and
  `authenticated`; nothing readable by default.
- `authenticated`: SELECT for admins only. INSERT and UPDATE for admins only,
  column-scoped, each paired with that SELECT policy (rule 11). No DELETE.
- Status changes are guarded by a trigger: only the allowed transitions, and
  approval only by an admin with `can_view_cost`. A direct UPDATE cannot skip
  it. The dashboard's own policy allows it to set draft, rejected and disabled
  only: validating and approving belong to the backend (section E).
- `hotel_agent`: column-scoped SELECT on batches (id, hotel, status,
  `approval_seq`), on nights, and later on the FAREAST tables, with
  `USING (true)` policies as in 0027. No write.
- Audit: security-definer triggers write to `audit_log` for batch status and
  review choices, row price edits, `sell_adjustment_bps`, meal price,
  `bot_sellable` and the nationality list. The allow-list in
  `audit_log_select_admin_only` is restated from 0035 with these pairs added.
  None carries cost.

## E. Approval flow

- States: draft → validated → approved ⇄ disabled. Draft or validated →
  rejected. An edit after validation returns the batch to draft.
- Validation is deterministic. It blocks on:
  - a night that falls below the floor at a neutral demand factor (1.0):
    base × adjustment < cost + minimum profit. This also covers a base below
    cost. The minimum profit used is the band for the night's lead time on
    the validation date, the highest floor that night can still have
    (confirmed by the owner, 2026-10-04);
  - SUP and SUITE at different prices for the same period;
  - an unresolved period-end choice or an unconfirmed missing year;
  - an unknown room type, or overlapping periods inside the batch;
  - a closed period that overlaps nights with sellable rooms.
- It warns, without blocking, on:
  - nights that fall below the floor only under a demand factor below 1.0
    (these would raise the configuration error at quote time if demand drops
    that far);
  - nights with no allotment (the price sits unused until rooms are entered).
- The reviewer sees the period rows, the expanded night count, and per night
  the new base price against the price it replaces or "no sheet price". The
  preview comes from the same function the approval uses to write the nights.
- Validation and approval are built in Python (`services/`), with PR 2, not
  as a SQL function (owner, 2026-10-04). The floor check needs the night's
  season (Hijri conversion, which CLAUDE.md rule 6 keeps in `lib/hijri.py`)
  and the price-rule chain, so it reuses pricing's own resolvers instead of
  a second copy of them in SQL. The dashboard reaches it through the agent's
  internal endpoint, as staff replies do.
- Approval is one transaction. It:
  - takes a per-hotel lock, so two batches for the same hotel cannot be
    approved concurrently, then locks the batch row;
  - requires the validated state and a matching fingerprint;
  - re-runs the full validation, not only the fingerprint check, so a change
    to seasons, `sell_adjustment_bps`, minimum-profit bands, costs or room
    types since validation cannot slip through;
  - writes the nights, then sets the batch approved (the guard draws
    `approval_seq`);
  - fails on a second call. It writes no room count and touches no inventory
    row.
- A refusal cannot both roll back and return the batch to draft in one
  transaction. So on a failed re-validation the approval transaction is
  rolled back, a second short transaction sets the batch to draft, and the
  findings are returned to the caller as a result, not raised.
- Cost-derived findings are shown only to users with `can_view_cost`.

## F. Agent

Sheet prices need no agent change: `get_quote` is unchanged for a room-only
stay, and a configuration error already ends in the escalation funnel.

FAREAST (each item needs approval before building):
- `get_quote` gets three optional arguments: package (closed list), guests,
  and customer nationality (closed list of country codes).
- The dispatch code stores the claimed nationality on the conversation and
  checks it against the package's list. The list never enters the prompt.
- Fixed unpriced results, no price in any of them:
  - `nationality_required`: the model asks the customer;
  - `package_not_eligible`: the model offers the room-only price;
  - `package_not_priced`: the hotel has no meal price; the code opens a staff
    escalation, once per turn.
- Guests are checked against the room type's capacity × rooms.
- The quote result gives a room total, a meal total and a grand total, all
  rendered by code. The output guard's allowed amounts must include them,
  which is a guard change.
- Tests: adversarial cases for nationality claims (role-play, authority,
  switching the claim, injection, language switch) and eval scenarios.

## G. PR order

1. BUILT. Schema: batches, rows, nights, access rules, audit, transition
   guard (migrations 0037 and 0038). Integration tests on real Postgres.
   Nothing reads it, and the dashboard has no path to validated or approved.
2. Pricing: `sell_adjustment_bps`, the sheet lookup, the formula with base
   and cost, `price_source`, the validator migration. Validation and approval
   in Python, with their tests (concurrent approval of two batches for one
   hotel, re-validation refusal, second-call failure, precedence by
   `approval_seq`, disable revealing the previous batch). 100% coverage.
   Inert until a batch is approved.
3. Admin: manual batch entry (the dashboard's write functions), validate,
   preview, approve, disable; the adjustment field on the price-rules screen.
   The feature is usable here, before any parser.
4. DEFERRED indefinitely. Base-only rule: cost becomes nullable, allotment
   entry accepts a night without cost, pricing gets the no-cost branch.
5. Parser, source files table, upload limits (10 MB).
6. FAREAST schema and admin editor (meal price, nationality list).
7. FAREAST pricing and quote columns; output guard amounts.
8. Agent: arguments, nationality state, checks, escalation, tests.

Stop-sale enforcement in `inventory/` stays a separate approval; until then
validation blocks a closed period over sellable rooms.

## Later, not in scope now

Editing `sell_adjustment_bps` or the minimum-profit bands after approval can
push already approved nights below the floor. The price-rules screen should
warn about the affected future nights before saving. Own PR, after PR 3.

## Open questions

Owner questions: all answered on 2026-10-04 and folded in above.

For the client (one list):
1. What the number in a sheet's FAREAST column means (room, meal or total).
   Until answered it is shown for review only and never written as a price.
2. Do FAREAST meal prices differ for children, and by what age bands?
3. How mixed-nationality groups are handled.
4. Whether a customer may change the claimed nationality.
5. Whether an uploader may approve their own batch (allowed for now).
6. Rounding of displayed prices (already open in §5); adjustments make
   unrounded amounts more common.
