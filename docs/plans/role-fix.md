# Plan: dedicated database roles and non-root services (role-fix)

**Status: approved by the owner on 2026-09-25 as written. The repository changes are in PR #58 (draft while in progress).**
Written 2026-09-24. This file contains no secrets: only role names, variable names and paths.

**The gates in this plan still apply and are not relaxed by the approval:** the final SQL of migration 0027 is shown to the owner before it is applied, and it is applied only on the owner's explicit go-ahead at that moment; every host-level step is shown before it is run; the assistant creates no credential and touches no `.env`; nothing is restarted without the owner's confirmation. As of this update, **migration 0027 has not been applied to any database and no host-level step has been run.**

## 1. Decisions on record (owner, 2026-09-24)

- **Two roles:** `hotel_agent` (the webhook process) and `hotel_worker` (the timer).
- **Migration 0027** is applied to the Supabase project `hotel-sales-agent-dev` by the assistant, **only after showing the final SQL and receiving the owner's explicit go-ahead at that moment**.
- **Role-level `statement_timeout`** is included. The **deny-triggers** on `quotes` and `audit_log` are skipped.
- **The non-root prerequisite is part of this PR:** Python 3.14 outside `/root`, dedicated non-root service users, and `worker.env`. **The owner creates and sets every credential.** The host-level steps are shown before any is run.
- **`worker.env` stays `0600 root:root`.** systemd reads `EnvironmentFile=` as root before dropping privileges. Evidence on the host: `/etc/hotel-admin/admin.env` is `0600 root:root` while `hotel-admin` runs as `www-data` and is active.
- **The "no new users" rule in `docs/deployment.md` (rule 5) is lifted for dedicated service accounts only**, and the reason is recorded in that document (see section 6).
- **Working practice:** this PR is developed in a separate clone, never in a worktree of the live repository. The live tree is updated only by `git pull --ff-only` run as `www-data`.

## 2. Approval checklist (what the owner approves before implementation starts)

1. The design in section 4 and the grant manifest in section 5.
2. The PR contents in section 6.
3. The cutover order in section 7.
4. The draft host-level steps in section 8 (each is shown again, and none is run before approval).
5. The `statement_timeout` values in section 4 (proposed, not decided).

## 3. Verified facts (read-only, 2026-09-24)

- The backend connects as **`postgres`** through the Supabase shared pooler in session mode (port 5432), to `hotel-sales-agent-dev`.
- `postgres` owns all 17 tables and both views, has `BYPASSRLS` and `CREATEROLE` (not superuser), and can `UPDATE`, `DELETE` and `TRUNCATE` `quotes` and `audit_log`.
- Migration 0013's lockdown revokes from `service_role` only. It holds for that role, but nothing connects as it, so today it binds nothing. RLS is forced on 17 of 17 tables, but `postgres` bypasses it.
- No custom role exists. Default ACLs give `service_role` full rights on every new table `postgres` creates in `public` (migration 0012 closed that only for `anon` and `authenticated`).
- `agent.env` also holds `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY` and `SUPABASE_URL`; no Python code reads any of them.
- `hotel-agent` and `hotel-worker` run as **root** (no `User=`). `hotel-admin` runs as `www-data`.
- A non-root agent or worker is impossible today: `/root` is `drwx------`, the venv's interpreter lives under `/root/.local/share/uv/python/`, `.venv` is root-owned, and the system Python is 3.12 while the project needs 3.14.
- Supabase documentation: the shared pooler supports custom roles, keeps one pool per role, and a role with an expired `VALID UNTIL` breaks pooler authentication. The dotted username format is documented only for `postgres`.
- A role that writes a table whose `CHECK` calls a function needs `EXECUTE` on it (precedent: the `price_rules_is_valid_*` validators are granted to `authenticated`).

## 4. Design

1. **Roles.** `hotel_agent` and `hotel_worker`: `LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, `NOINHERIT`, `NOCREATEROLE`, `NOCREATEDB`, no memberships, owning nothing, **no password** (the owner sets it), **no `VALID UNTIL`**.
2. **Subject to RLS.** Explicit per-table, per-command policies `TO` the role, each write policy paired with a `FOR SELECT` (CLAUDE.md rule 11). The backend legitimately touches every row, so these policies cannot narrow rows; their value is fail-closed (a new table or command is denied by default). The `GRANT`s do the real narrowing, column by column for writes.
3. **The 0013 lockdown binds the new roles.** `quotes`: `SELECT` and `INSERT` only. `audit_log`: no grant (the backend never reads it). The erasure functions stay ungranted (no code calls them).
4. **Manifest test.** It fails if any `public` table is unclassified for a role, or if a role holds any privilege beyond the manifest.
5. **New roles are in no default ACL**, so future tables are invisible to them until a migration grants access.
6. **`statement_timeout`** is set with `ALTER ROLE ... SET` for each role. Approved values (owner, 2026-09-25): 10 s for `hotel_agent`, 30 s for `hotel_worker`. Whether the setting takes effect through the pooler is verified with `SHOW statement_timeout` on the first connection as each role.

## 5. Grant manifest (from the code in `services/`)

| Role | Table | Privileges | Source |
|---|---|---|---|
| agent | `conversations` | SELECT; INSERT (customer_phone); UPDATE (customer_phone, turn_count, active_quote_id, concession_count, last_message_at) | webhook.py:336, caps.py:237, session.py:91,121, context.py:68 |
| agent | `messages` | SELECT; INSERT (conversation_id, customer_phone, direction, whatsapp_message_id, body) | webhook.py:363,392, context.py:98, caps.py:256 |
| agent | `escalations` | INSERT (conversation_id, customer_phone, reason, notes); SELECT for RETURNING | enforcement.py:178 |
| agent | `token_usage` | SELECT; INSERT | caps.py:120,138,201 |
| agent | `quotes` | SELECT; INSERT only | compute.py:232; guard quotes.py:55 |
| agent | `allotments`, `room_night_inventory`, `seasons`, `price_rules`, `price_overrides` | SELECT | compute.py, demand.py, operations.py, dispatch.py:175, seasons.py, rules.py |
| worker | `holds` | SELECT; UPDATE (released_at) | hold_expiry.py:34, operations.py:308 |
| worker | `room_night_inventory` | SELECT; UPDATE (held, reserved) | operations.py:93,133 |
| worker | `allotments` | SELECT | joins at operations.py:31,135 |

- The worker needs `UPDATE` on `reserved` too, because `release_hold` sets it even when the delta is 0.
- The agent never calls `create_hold` or `confirm_hold`, so it gets no hold grants until the booking flow is wired.
- Nothing deletes or truncates anywhere. The backend never reads `hotels`, `room_types`, `bookings`, `app_users` or `audit_log`.
- Likely also needed: `EXECUTE` on `quotes_is_valid_night_record` and `quotes_all_nights_are_complete` for the agent (see section 9).

## 6. What the PR contains

- **Migration `0027_backend_roles.sql`:** idempotent role creation (a `DO` block, since roles are cluster-wide), grants, policies, and the role-level `statement_timeout`. No passwords.
- **Tests:** role attributes and memberships; the exact privilege manifest; rule-11 policy pairing; real denied `UPDATE`, `DELETE` and `TRUNCATE` on `quotes` and `audit_log`; real flows as the roles (webhook, quote insert, output guard, worker pass) through DSNs derived from `TEST_DATABASE_URL` by swapping the user (CI uses trust auth); `db_conn` stays privileged for seeding.
- **Comment fix:** `tests/conftest.py:125` claims tests run "at the same privilege level" as production, which becomes false.
- **Units in `ops/`:** `hotel-worker.service` and `hotel-agent.service` gain `User=`/`Group=`, the new interpreter path and hardening; the worker points at `worker.env`.
- **`docs/deployment.md`:** rule 5 is amended to allow dedicated service accounts for `hotel-agent` and `hotel-worker` only, recording the reason: the internet-facing admin process must not share a UID with the agent, or it could read the agent's process environment (database credential, LLM key, WhatsApp token). The non-root layout is documented.
- **`ARCHITECTURE.md`:** a subsection recording the role decisions. The status wording for the pricing, location and `FAREAST` approvals is handled by the separate docs PR #57, not by this PR.
- **Not touched:** `SETUP.md` (the owner's local change stays; its lines about `DATABASE_URL` will need an update later) and migration 0001's false comment (a migration that has run is never edited).

## 7. Cutover (who does what)

1. The PR merges. The migration file in the repo has no effect on any database yet.
2. **The assistant shows the final SQL; the owner gives an explicit go-ahead; the assistant applies migration 0027 to `hotel-sales-agent-dev`.** The roles then exist without passwords.
3. **The owner** sets the passwords (`ALTER ROLE ... PASSWORD ...`, in the Supabase SQL editor) and builds the connection strings: same host, port and database as today, with the username `<role>.<project-ref>`. The owner changes **`DATABASE_URL` in `/etc/hotel-admin/agent.env`** and creates **`/etc/hotel-admin/worker.env`** with its own `DATABASE_URL`. Nothing else changes: `admin.env` has no database URL, and `TEST_DATABASE_URL` stays `postgres`.
4. Before any restart, the assistant can read each new URL in a subshell without printing it and run read-only checks (current role, attributes, privileges against the manifest, `SHOW statement_timeout`).
5. The host-level steps in section 8 run, each shown to the owner first.
6. The `hotel-agent` restart needs the owner's explicit confirmation.
7. Recommended afterwards: remove the three unused `SUPABASE_*` variables from `agent.env`, and rotate the `postgres` password.

**Order:** database roles first, then the units, so the OS-user change and the role change land together.

## 8. Draft host-level steps for the non-root move (NOT RUN)

Every step is unexecuted and is shown to the owner first. Nothing touches the running agent until step 7.

1. **Service users:** `useradd --system --no-create-home --shell /usr/sbin/nologin hotel-agent`, and the same for `hotel-worker` (needs the docs amendment above).
2. **Python 3.14 outside `/root`:** `uv python install --install-dir /opt/hotel/python 3.14` (the `--install-dir` flag is verified in `uv python install --help`). Directory root-owned, mode 0755. Check that a non-root user can execute it.
3. **A new venv outside `/root` and outside the repo:** `uv venv /opt/hotel/venv --python <that python>`, then from the repo `VIRTUAL_ENV=/opt/hotel/venv uv sync --frozen --active` (`uv venv [PATH] --python`, `uv sync --active` and `--frozen` are verified in the help output; `--no-dev` to be checked at that step). The current `/srv/hotel-admin/app/.venv` and `/root/.local/bin/uv` stay in place, so the running agent is unaffected.
4. **Env files:** `agent.env` stays as is (`0600 root:root`); the owner creates `worker.env` (`0600 root:root`).
5. **Unit changes (in the PR, applied later):**
   - `hotel-worker.service`: `User=hotel-worker`, `Group=hotel-worker`, `EnvironmentFile=/etc/hotel-admin/worker.env`, `ExecStart=/opt/hotel/venv/bin/python -m services.worker`.
   - `hotel-agent.service`: `User=hotel-agent`, `Group=hotel-agent`, `ExecStart=/opt/hotel/venv/bin/uvicorn services.agent.main:app --host 127.0.0.1 --port 8000` (no `uv run`), with the same hardening as the worker (`NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome=yes`, `PYTHONDONTWRITEBYTECODE=1`).
   - The repo tree is already world-readable, so both users can read the code.
6. **Test the worker first:** apply the new worker unit, `systemctl daemon-reload`, `systemctl start hotel-worker.service`, read the journal, and check the process owner with `ps`.
7. **Agent cutover (owner's confirmation):** apply the new agent unit, `systemctl daemon-reload`, `systemctl restart hotel-agent`, verify with `systemctl show hotel-agent -p User` and `ps -o user=`.
8. **Rollback at every step:** restore the previous unit files from `ops/` (git history), `systemctl daemon-reload`, restart. The old venv and the root-owned `uv` stay until the new setup is verified.

## 9. What was unverified, and what is still unverified

There is no Postgres on this host, so the database tests run in CI only. Settled in CI (2026-09-25):

- **`EXECUTE` on the two `quotes` `CHECK` validators is required.** A deliberate-breakage run on a throwaway branch (closed, not merged) removed the grants and the agent's quote test failed with "permission denied for function".
- **The paired `SELECT` policy is required for the `conversations` upsert.** Removing it made 32 of the webhook tests fail as `hotel_agent` with "new row violates row-level security policy". The same run showed that dropping the worker's `UPDATE` on `reserved` breaks the worker pass.
- **Identity columns need no sequence privilege:** the whole webhook suite and the worker pass run as the roles, and the manifest test proves neither role holds any sequence privilege.
- **Every agent code path works under the grants:** the webhook integration suite runs a second time with the app connected as `hotel_agent`, and passes.

Still unverified (needs the first real connection to the hosted database):

- The custom-role pooler username `<role>.<project-ref>`: confirm on the first connection.
- The role-level `statement_timeout` taking effect through the pooler (`SHOW statement_timeout` as each role).
- The `--no-dev` flag and the venv placement in section 8, step 3.
