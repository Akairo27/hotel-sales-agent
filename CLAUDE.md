# CLAUDE.md — Working Rules for This Repository

This system sells real hotel rooms for real money to real customers.
A bug here does not produce a stack trace — it produces a financial loss
or a room sold twice. Behave accordingly.

Read `ARCHITECTURE.md` before writing any code. Do not deviate from it.

---

## 0. THE INVIOLABLE RULES

These are not preferences. Violating any of them is a defect, regardless
of whether tests pass.

1. **The LLM never computes a price.** All prices come from
   `pricing-service`. If you find yourself writing a prompt that asks the
   model to calculate, discount, or compare prices — stop. That logic
   belongs in code.

2. **Cost never enters the LLM context.** Not in the system prompt, not in
   a tool result, not in a log the model can read. `min_allowed_price` is
   the only floor-related value the model may see.

3. **Every inventory mutation happens inside a transaction with
   `SELECT ... FOR UPDATE`.** Application-level availability checks are
   advisory only. The DB constraint is the source of truth.

4. **Never remove, weaken, or bypass `inventory_never_oversold`.** If a
   test fails because of it, the test or the code is wrong — not the
   constraint.

5. **All money is stored as integers (halalas).** Never float, never
   decimal-as-float. `1250` means 12.50 SAR. Any float in a money path is
   a defect.

6. **All dates are stored in UTC.** Hijri conversion happens in exactly
   one module (`lib/hijri.py`). Nowhere else. Never hand-roll a Hijri
   calculation inline.

7. **No business logic in n8n.** n8n handles notifications and side
   integrations only. Pricing, inventory, booking, and negotiation logic
   live in FastAPI services under version control.

8. **The output guard runs on every outbound message.** Never add a code
   path that sends text to a customer without passing through it.

9. **`service_role` keys never leave the server.** Not into Next.js client
   code, not into n8n, not into logs.

10. **RLS is enabled on every table, deny-by-default.** A new table
    without an RLS policy is an incomplete migration.

11. **Every `FOR INSERT` or `FOR UPDATE` policy on a table must be paired
    with a `FOR SELECT` policy covering the same rows.** Without it, the
    write fails -- either the row is silently invisible to the write's own
    `WHERE`/upsert resolution (zero rows affected, no error), or, for
    `INSERT ... ON CONFLICT DO UPDATE`, Postgres denies the statement
    outright, even when no actual conflict occurs. Confirmed the hard way
    three times: PR D (`admin_set_allotment_cost`), PR E
    (`admin_upsert_price_rule`), and PR F
    (`admin_upsert_price_overrides`'s `ON CONFLICT DO UPDATE`, the
    broadest case -- it failed even on a plain, non-conflicting insert).
    Add the `FOR SELECT` policy in the same migration as the write policy,
    never as an afterthought once a write mysteriously no-ops.

12. **Never leave a customer in silence.** A customer message the agent
    accepts ends in one of two ways: the reply is delivered, or the turn
    goes through the one funnel (`services/agent/webhook.py`'s
    `_escalate_and_notify`), which always attempts BOTH the standard
    fallback message to the customer AND a human escalation, each
    independently of the other. A failed turn -- any exception, a blank or
    unsendable reply, a failed send -- is never allowed to end any other
    way; never add a code path that ends a turn with neither a reply nor
    that funnel. What no code can fix (WhatsApp refusing every send, the
    database being down, a hard kill) and the few deliberate silences are
    listed in `ARCHITECTURE.md` §7 ("لا صمت"), each with the owner's
    approval; a new entry needs the same.

---

## 1. Forbidden Shortcuts

These are the failure modes of a coding agent under pressure. Every one
of them is an automatic rejection, no exceptions, no "just for now".

**Never fake work:**
- No stub that returns hardcoded or sample data in place of a real
  implementation. If you cannot implement it, say so and stop.
- No `TODO`, `FIXME`, or `pass  # implement later` in merged code.
- No commented-out code. Delete it; Git remembers.
- Never report a task complete without running the tests and stating the
  actual result.

**Never make a problem disappear instead of fixing it:**
- **Never delete, skip, or weaken a failing test to get green.** A failing
  test is information. Fix the code, or stop and report.
- Never hardcode a value so a test passes.
- Never widen an exception handler to swallow an error you do not
  understand.
- Never add `# type: ignore`, `any`, `eslint-disable`, or `noqa` to
  silence a warning. Fix the underlying cause or ask.
- **Never drop or alter a DB constraint to make an insert succeed.** The
  constraint is correct; your data is wrong.

**Never invent:**
- Do not call a library function, API method, or config key you have not
  verified exists. Check the installed version's actual signature — do
  not rely on recall.
- Do not invent a field, table, or column. Check the migrations.
- If you are unsure whether something exists, say "I need to verify this"
  and verify it. Guessing in a payments or inventory path is unacceptable.
- Do not fabricate test results, coverage numbers, or benchmark figures.

**Never quietly expand or shrink scope:**
- Do not rewrite code you were not asked to change, even if it is worse
  than what you would write.
- Do not "improve" naming, structure, or style outside your task.
- Do not silently drop a requirement because it was hard. Report it.

---

## 2. Craft Standards

The client will hire someone to review this code. Write for that reader.

- **Functions do one thing.** If a function needs a comment to explain its
  sections, split it.
- **Soft limits:** ~50 lines per function, ~400 per file. Exceeding either
  is a signal to decompose, not a rule to route around.
- **Names say what they mean.** `calculate_floor_price` not `calc2` or
  `helper`. No abbreviations beyond widely understood ones.
- **No magic numbers.** `MAX_CONCESSIONS = 3` in a constants module, not
  `3` inline.
- **Consistency beats preference.** Match the surrounding code's patterns,
  even if you would do it differently.
- **Errors are explicit.** Custom exception types with clear names —
  `InsufficientInventoryError`, not `Exception("error")`.
- **One way to do each thing.** If a helper exists, use it. Do not write a
  second date parser, a second money formatter, a second HTTP client.
- Public functions have a docstring stating what, why, and what raises.

---

## 3. Automated Gates

These run in CI. A red build blocks merge. Do not attempt to bypass,
disable, or work around them.

| Gate | Tool | Threshold |
|---|---|---|
| Lint + format | ruff | zero errors |
| Types | mypy `--strict` | zero errors |
| Tests | pytest | all pass |
| Coverage — `pricing/`, `inventory/` | pytest-cov | **100%** |
| Coverage — everything else | pytest-cov | ≥ 80% |
| Secrets | gitleaks | zero findings |
| Dependency vulnerabilities | `pip-audit`, `npm audit` | zero high/critical |
| Frontend | eslint + tsc `strict` | zero errors |
| Migrations | sqlfluff | zero errors |

Pre-commit hooks run lint, format, types, and secret scan locally.

**If a gate fails, fix the cause. Never adjust the threshold.**

### Dependency Rules

Supply chain attacks on npm and PyPI are a live threat. These are not
optional.

- **Never add a dependency without asking.** State the package, its
  purpose, its weekly download count, and its last release date.
- **Pin exact versions.** No `^`, no `~`, no `*`. `fastapi==0.115.0`,
  not `fastapi>=0.115`.
- **Lockfiles are committed** and are the source of truth. Install with
  `npm ci` and `uv sync --frozen` / `pip install --require-hashes`.
  Never `npm install` in CI.
- **`ignore-scripts` stays enabled.** Postinstall scripts are the primary
  execution vector for compromised packages. Do not disable it, and do not
  add a package that cannot function without them.
- **Prefer the standard library.** A dependency that saves ten lines is
  not worth the attack surface.
- Never add a package published in the last 90 days without explicit
  approval, and never one whose name closely resembles a popular package.

---

## 4. Scope Discipline

- **Do exactly what was asked.** Do not add features, endpoints, or
  abstractions that were not requested.
- **Do not refactor code you were not asked to touch.** If you see a
  problem elsewhere, report it in your summary — do not fix it silently.
- **Do not add dependencies without asking first.** State the package,
  why it is needed, and what it replaces.
- **Do not change the database schema without asking.** Schema changes are
  migrations, reviewed separately.
- **If the request is ambiguous, ask.** Do not guess and proceed. A wrong
  assumption in pricing logic is expensive.

---

## 5. Repository Layout

```
/services
  /agent          FastAPI — WhatsApp webhook, conversation, LLM orchestration
  /pricing        FastAPI — deterministic price computation
  /inventory      FastAPI — allotment, holds, bookings
  /worker         scheduled jobs: hold expiry, alerts, accounting sync
/admin            Next.js dashboard
/db
  /migrations     numbered SQL migrations — forward only
  /seeds          test data only, never production values
/lib
  hijri.py        the ONLY Hijri/Gregorian conversion
  money.py        integer money helpers
/tests
  /unit
  /integration
  /adversarial    prompt-injection and negotiation-abuse cases
/docs
```

---

## 6. Testing Requirements

No PR merges without tests for the code it touches.

**Mandatory coverage — these are non-negotiable:**

- **Pricing:** every season boundary (start-inclusive, end-exclusive),
  stays spanning 2+ seasons, inheritance resolution order, floor never
  below cost, demand factor never affecting `min_allowed`.
- **Inventory:** concurrent booking of the last available room must
  result in exactly one success and one clean failure. Write this test
  with real concurrent transactions, not mocks.
- **Holds:** expiry releases inventory exactly once, never twice.
- **Idempotency:** the same WhatsApp `message_id` processed twice must
  produce one booking.
- **Adversarial:** the agent must not quote below `min_allowed` under
  role-play, authority claims, emotional pressure, instruction injection,
  or language switching. Add a new case every time a real attempt is
  observed in production.

**Pricing functions must be pure.** Same inputs, same output, no I/O,
no clock reads inside the calculation — pass `now` in as a parameter.

---

## 7. Git Workflow

- Branch per unit of work: `feat/`, `fix/`, `chore/`, `docs/`
- Conventional commits: `feat(pricing): add season boundary resolution`
- **Never commit directly to `main`.**
- PR required, with: what changed, why, what was tested, what was not.
- **Never commit secrets.** `.env.example` only, with placeholder values.
- Never force-push a shared branch.
- Migrations are forward-only. Never edit a migration that has run.

---

## 8. Code Standards

- Type hints on every function signature. Pydantic models for all
  request/response bodies.
- No bare `except:`. Catch specific exceptions.
- Every external call (WhatsApp, Gemini, payment, accounting) has a
  timeout and explicit failure handling. A hanging call must not hang a
  customer conversation.
- Structured logging (JSON). **Never log cost, secrets, or full customer
  messages containing personal data.**
- Log every price decision with its `quote_id` for traceability.
- Comments explain *why*, not *what*. Do not narrate the code.

---

## 9. LLM Integration Rules

- The model is accessed through **one interface module**
  (`services/agent/llm/`). No direct SDK calls scattered across the code.
- **Pin the exact model version.** Never use a floating alias.
- Tool definitions live in one file, versioned, and reviewed.
- Conversation state (concession count, active quote, escalation status)
  lives **in the database**, never inferred from chat history. The model
  can be manipulated; the database cannot.
- Cap conversation turns. Beyond the cap, escalate to a human.
- Cap token spend per conversation and per number per day.

---

## 10. When You Are Unsure

Stop and ask. Specifically, always ask before:

- Changing anything in `pricing/` or `inventory/`
- Modifying a database constraint or RLS policy
- Adding a new tool the LLM can call
- Changing the output guard
- Anything touching payment or booking confirmation

State the tradeoff and wait. Do not proceed on assumption in these areas.

---

## 11. Definition of Done

A task is done when:

- [ ] Tests written and passing, including edge cases
- [ ] No new dependency added without approval
- [ ] RLS policy exists for any new table
- [ ] No secrets, no cost values, no floats in money paths
- [ ] Migration is forward-only and reversible in intent
- [ ] PR description states what was NOT covered
- [ ] `ARCHITECTURE.md` updated if the design changed

"It runs" is not done.
