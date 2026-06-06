# Codex Intervention Log

This file documents Codex interventions in this project.

For each intervention, add a dated entry with:

- request context
- files read or modified
- changes made
- checks run
- limits or follow-ups to plan

## 2026-05-21

### Backend Red-Team Hardening

Context:

- User request: immediately fix in code the vulnerabilities identified in the red-team report.

Files read:

- `main.py`
- `config.py`
- `README.md`
- `.env.example`
- `migrations/*`
- Local dashboard `setter-dashboard-saas` read-only for context.

Files modified:

- `main.py`
- `config.py`
- `.env.example`
- `README.md`
- `migrations/add_user_scope_to_ai_tables.sql`
- `AGENT_LOG.md`

Changes made:

- Replaced global CORS with a configurable allowlist through `CORS_ALLOWED_ORIGINS`.
- Made Meta WhatsApp signature mandatory on the inbound webhook.
- Added `user_id` scoping on `insights` and `prompt_versions` reads/writes.
- Added a migration for `user_id`, indexes, and RLS on `conversations`, `insights`, and `prompt_versions`.
- Scoped the WhatsApp debug endpoint by `user_id`.
- Deduplicated WhatsApp webhooks using `message_id` stored in history.
- Persisted auto messages before external sending, then marked them `sent` after success.
- Validated AI replies before sending or refinement: non-empty, length-limited, authorized links only.
- Removed provider response bodies from logs.
- Based pending refinement on the message stored in the database instead of the browser payload.
- Added size limits on the main inbound payloads.

Checks:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py config.py prompts.py test.py`
- Imported `main.app` and checked route loading.
- `git diff --check`

Limits and follow-ups:

- Run `migrations/add_user_scope_to_ai_tables.sql` in Supabase before deploying this backend.
- Backfill older `insights` and `prompt_versions` rows with `OWNER_USER_ID` if they must remain visible in the dashboard.
- Configure `CORS_ALLOWED_ORIGINS`, `ENVIRONMENT=production`, and `META_APP_SECRET` in Railway.
- The SaaS dashboard lives in a sibling repo/folder; no change was applied there in this pass.

### WhatsApp Cloud API Integration

Context:

- User request: add WhatsApp in addition to Instagram and establish a multi-channel architecture.

Files read:

- `main.py`
- `config.py`
- `README.md`
- `.env.example`
- `migrations/env_vars_to_add.md`
- `test.py`

Files modified:

- `main.py`
- `config.py`
- `.env.example`
- `.gitignore`
- `README.md`
- `migrations/env_vars_to_add.md`
- `migrations/add_whatsapp_channel.sql`
- `test.py`
- `AGENT_LOG.md`

Changes made:

- Added WhatsApp Cloud API configuration variables.
- Added a multi-channel migration for `channel`, `external_contact_id`, `phone_e164`, `last_inbound_at`, and `transport_metadata`.
- Extracted shared inbound message handling for Instagram and WhatsApp.
- Added WhatsApp sending through the Graph API `/{phone_number_id}/messages`.
- Added `GET /webhooks/whatsapp` and `POST /webhooks/whatsapp`.
- Added optional `X-Hub-Signature-256` verification with `META_APP_SECRET`.
- Fixed `disabled` mode so it stores history without generating a reply.
- Extended dashboard and follow-up data with channel and manual `wa.me` or `ig.me` links.

Checks:

- `PYTHONPYCACHEPREFIX=.pycache python3 -m py_compile main.py config.py test.py`
- FastAPI import and WhatsApp route registration check.

Limits and follow-ups:

- Run `migrations/add_whatsapp_channel.sql` in Supabase before deploying this backend.
- Configure Railway WhatsApp variables and the Meta webhook.
- Follow-ups outside the 24h window remain supervised until approved WhatsApp templates are added.

## 2026-05-01

### Railway Webhook Secret Configuration

Context:

- User request: configure the required next step after webhook protection.

Files read:

- `AGENT_LOG.md`
- `.env` in the main project folder, without displaying secrets

Files modified:

- `.gitignore`
- `AGENT_LOG.md`

Actions made:

- Checked the local Railway link: project `setter-agent`, environment `production`, service `setter-agent`.
- Added `WEBHOOK_SECRET` in Railway from the local `.env` value.
- Verified `WEBHOOK_SECRET` presence through `railway run`, without displaying its value.
- Added `.DS_Store` to `.gitignore`.
- First `railway up --detach` attempt was interrupted because it did not progress after indexing.
- Second `railway up --detach --verbose` attempt was interrupted because it stayed blocked on `Indexing...`.
- Third `railway up --ci` attempt was interrupted after a prolonged block on `Indexing...`.
- Created local commit `Secure webhook with secret header`.
- `git push origin main` attempt was rejected by GitHub with a 403 write-permission error.

Checks:

- `railway status` confirmed the project, environment, and service.
- `railway run sh -c 'test -n "$WEBHOOK_SECRET" ...'` confirmed the variable exists in production.
- Automatic deployment could not be completed from Codex because of the `railway up` block and GitHub write rejection.

Limits and follow-ups:

- Configure the tool calling the webhook to send the `X-Webhook-Secret` header with this same value.
- If the calling tool cannot add a custom header, adapt the webhook.
- Give this workstation valid GitHub write access or launch the Railway deployment manually from the UI.

### Webhook Protection

Context:

- User request: apply the recommended next step, meaning secure the webhook.

Files read:

- `main.py`
- `test.py`
- `AGENT_LOG.md`

Files modified:

- `main.py`
- `test.py`
- `AGENT_LOG.md`
- `.env` in the main project folder, without logging the secret value

Changes made:

- Added the `WEBHOOK_SECRET` environment variable.
- Added mandatory `X-Webhook-Secret` header verification on `POST /webhook`.
- Added HTTP 401 if the provided secret is missing or incorrect.
- Added clear HTTP 500 if `WEBHOOK_SECRET` is not configured.
- Added clear HTTP 500 if `ANTHROPIC_API_KEY` is not configured.
- Updated `test.py` to send `X-Webhook-Secret` from the local environment.
- Added a local `WEBHOOK_SECRET` value in `.env`.
- Replaced `str | None` with `Optional[str]` for compatibility with the local virtualenv Python.

Checks:

- Syntax check with `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py test.py`.
- Check in the project virtualenv with `/Users/thomasmahe/setter-agent/.venv/bin/python -m py_compile main.py test.py`.
- FastAPI `TestClient`: call without secret = 401, call with wrong secret = 401.

Limits and follow-ups:

- Configure `WEBHOOK_SECRET` in Railway and in the local `.env`.
- Configure the tool calling the webhook to send the `X-Webhook-Secret` header.
- Add error handling around the Anthropic call next.

### Intervention Log Creation

Context:

- User request: create a file documenting everything Codex does in this project.

Files read:

- `main.py`
- `README.md`
- `test.py`
- `pyproject.toml`
- `requirements.txt`
- `railway.json`

Files modified:

- `AGENT_LOG.md`

Changes made:

- Added a dedicated file for tracking Codex interventions.
- Defined a simple format for future entries.
- Added the first entry documenting the creation of this log.

Checks:

- No runtime check required; documentation-only change.

Limits and follow-ups:

- Future interventions should be added to this file over time.

## 2026-05-10

### AI Follow-Up Backend Foundation

Context:

- User request: connect the Follow-Up page to the backend and start generating follow-ups through the agent.

Files read:

- `main.py`
- `AGENT_LOG.md`
- `app/relance/page.tsx` and `lib/api.ts` in `setter-dashboard-ttr`

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- Added timestamps to new messages stored in `history`.
- Cleaned messages sent to Claude so `timestamp` metadata is not passed.
- Added `GET /follow-ups/due` to list due follow-ups according to the 23h, D+3, and D+10 thresholds.
- Added `POST /follow-ups/preview` to generate a follow-up proposal with Claude.
- Automatic ManyChat sending is not connected yet.

Checks:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` in `setter-dashboard-ttr`

Limits and follow-ups:

- Older conversations without timestamps use `created_at` as a fallback.
- Next step will be validating proposals in the dashboard, then connecting auto-send only before 24h.

### Persistent Agent Link Options

Context:

- User request: Calendly and sales page links must be agent options, not only playground test links.

Files read:

- `main.py`
- `AGENT_LOG.md`
- `app/agent/page.tsx` in `setter-dashboard-ttr`

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- Added internal markers `AGENT_OPTIONS_START` / `AGENT_OPTIONS_END` in the active prompt.
- Added `GET /agent-links` to read links from the active prompt.
- Added `PATCH /agent-links` to create a new active prompt version with agent links.
- Kept playground injection so tests immediately use the entered fields.

Checks:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` in `setter-dashboard-ttr`

Limits and follow-ups:

- Each option save creates a new `prompt_versions` entry with `source=agent-options`, preserving history and enabling restore.
- Restoring an older prompt version can restore old links or remove options if that version did not contain them.

### Agent Playground Link Parameters

Context:

- User request: keep the current Agent page and add only the Calendly link and sales page link.

Files read:

- `main.py`
- `AGENT_LOG.md`
- `app/agent/page.tsx` in `setter-dashboard-ttr`

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- Added optional `calendly_url` and `sales_page_url` fields to the `POST /playground` payload.
- Injected those links into the system prompt only for the dashboard playground.
- No change to `POST /webhook` or to the saved active prompt.

Checks:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` in `setter-dashboard-ttr`

Limits and follow-ups:

- These links affect only Agent page tests. To change live webhook behavior, apply a new prompt version or add shared configuration.

### Progressive Dashboard Conversation Loading

Context:

- User request: reduce long dashboard loads and add animations to avoid a blocked feeling.

Files read:

- `main.py`
- `AGENT_LOG.md`
- Frontend files in `setter-dashboard-ttr`

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- Added `GET /conversations/summary` to return a lightweight list without full history.
- Added `GET /conversations/{conversation_id}` to load full detail only when clicking a prospect.
- Kept `GET /conversations` for compatibility with the old behavior.

Checks:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` in `setter-dashboard-ttr`

Limits and follow-ups:

- Deploy the backend before or at the same time as the frontend, because the dashboard uses the new `/conversations/summary` endpoint.
- Test on the live dashboard that the list loads faster and history appears on click.

### Dashboard Conversation Endpoint Alignment

Context:

- User request: test and fix consistency between the dashboard and the agent if needed.
- Observation: the dashboard calls status and deletion endpoints with the Supabase conversation id.

Files read:

- `main.py`
- `AGENT_LOG.md`
- `lib/api.ts` in `setter-dashboard-ttr`

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- Changed `PATCH /conversations/{...}/status` to filter Supabase by `id`.
- Changed `DELETE /conversations/{...}` to filter Supabase by `id`.
- ManyChat endpoints `POST /webhook`, `POST /activate`, and `POST /deactivate` were not changed.

Checks:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` in `setter-dashboard-ttr`

Limits and follow-ups:

- Test in the live environment that status changes and deletion update the expected Supabase rows.
- Separately verify `username` / `subscriber_id` alignment for dashboard Instagram links.

### Secure Manual H23 Follow-Up Send

Context:

- User request: handle the first automatic follow-up, the one before the Instagram/ManyChat 24-hour limit.

Files read:

- `main.py`
- `AGENT_LOG.md`
- Frontend files in `setter-dashboard-ttr`

Files modified:

- `main.py`
- `AGENT_LOG.md`
- `app/relance/page.tsx` in `setter-dashboard-ttr`
- `lib/api.ts` in `setter-dashboard-ttr`

Changes made:

- Added `POST /follow-ups/{conversation_id}/send-auto-23h`.
- The endpoint verifies that the conversation is due in `auto_23h`, meaning between 23h and 24h after the last prospect message.
- Generated the follow-up message with Claude through the existing follow-up prompt.
- Sent through the ManyChat `sendContent` API with `username` as the ManyChat subscriber id.
- Added the sent message to `history` with `follow_up_stage=auto_23h` to avoid double sending.
- The `/relance` page now shows a `Send H23` button for due automatic follow-ups.

Limits and follow-ups:

- This build adds dashboard-triggered H23 sending, not yet an autonomous cron job.

## 2026-05-20

### SaaS Dashboard Login Stabilization

Context:

- User request: fix SaaS dashboard login issues and align the backend with Supabase Auth.

Files read:

- `main.py`
- `README.md`
- Frontend files in `setter-dashboard-saas`

Files modified:

- `main.py`
- `README.md`
- `AGENT_LOG.md`
- `proxy.ts`, `app/login/page.tsx`, `lib/api.ts`, `lib/config.ts`, `lib/supabase.ts`, `lib/supabase/client.ts`, `.env.example`, `migrations/frontend_env_vars.md` in `setter-dashboard-saas`

Changes made:

- Replaced the empty Next middleware in the dashboard with a Next 16-compatible `proxy.ts`.
- Added server protection for dashboard routes and automatic login/CRM redirects based on the Supabase session.
- Cleaned up the login page: Next router redirect, removed auth result log, trimmed email.
- Added explicit errors if `NEXT_PUBLIC_API_URL` or Supabase Auth are not configured.
- Backend hardening: reject JWTs that do not match `OWNER_USER_ID` when it is configured.
- Added `user_id` filters to dashboard conversation reads and mutations.
- Updated documented frontend variables and backend dashboard auth docs.

Checks:

- `npm run build` in `setter-dashboard-saas`
- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py config.py prompts.py test.py`

Limits and follow-ups:

- `npm run lint` remains blocked by a broken local ESLint install in `node_modules`; fix by reinstalling dependencies if needed.
- Verify on Vercel that `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_SUPABASE_URL`, and `NEXT_PUBLIC_SUPABASE_ANON_KEY` are configured.
- Possible next step: add a Railway cron that calls H23 sending automatically every few minutes.

### ManyChat Endpoint For D+1 Follow-Up

Context:

- User request: connect the D+1 follow-up directly in ManyChat with an External Request.
- ManyChat called `/follow-ups/manychat-auto-23h` and received `404 Not Found`.

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- Added `POST /follow-ups/manychat-auto-23h`.
- Expected payload: `{ "subscriber_id": "..." }`.
- Looked up the conversation through `username = subscriber_id`.
- Verified that the due follow-up is `auto_23h`.
- Generated the AI message then returned JSON `{ "message": "...", "conversation_id": "...", "stage": "auto_23h" }`.
- Marked history with `follow_up_stage=auto_23h` and `source=follow_up_manychat` to avoid duplicates.

Check:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`

Limit:

- If the ManyChat test contact is not currently between 23h and 24h since the last message, the endpoint returns `409 Auto 23h follow-up is not due`.

### Stable Response For ManyChat Mapping

Context:

- ManyChat needs a `200 OK` response with the `message` field to configure Response mapping.
- With a contact outside the H23 window, the endpoint returned `409`, blocking mapping.

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- `POST /follow-ups/manychat-auto-23h` now always returns stable JSON.
- Non-eligible case: `{ "ok": false, "message": "", "reason": "..." }`.
- Eligible case: `{ "ok": true, "message": "...", "conversation_id": "...", "stage": "auto_23h", "reason": null }`.

Check:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`

ManyChat next steps:

- Map `message` to `relance_j1_message`.
- Add a `relance_j1_message is not empty` condition before the Instagram send block.

### ManyChat D+1 Follow-Up Test Mode

Context:

- User request: test the ManyChat flow in real time without waiting for a prospect in the H23 window.

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- Added `force_test` and `test_only` to the `POST /follow-ups/manychat-auto-23h` payload.
- `force_test=true` allowed generating a follow-up even if the conversation was not between 23h and 24h.
- `test_only=true` prevented marking the follow-up as sent in Supabase.
- The response included `reason: "test_mode"` and `test_only: true` in test mode.

Check:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`

Important:

- Remove `force_test` and `test_only` from the ManyChat body before final flow publication.

### Remove ManyChat D+1 Test Mode

Context:

- User request: remove test mode after flow validation.

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- Removed `force_test` and `test_only` from the ManyChat payload.
- `POST /follow-ups/manychat-auto-23h` no longer generates follow-ups outside H23 eligibility.
- Supabase marking is systematic again when a follow-up is generated.

Check:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`

### Follow-Ups Based On The Active Prompt

Context:

- User request: use a hybrid version for follow-ups, with the specialized follow-up prompt + active meta-prompt.

Files modified:

- `main.py`
- `AGENT_LOG.md`

Changes made:

- `generate_follow_up_message()` now loads `get_active_prompt()`.
- The active prompt is injected into the context given to Claude for follow-ups.
- The follow-up system prompt remains specialized to keep the message short, natural, and non-aggressive.

Check:

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
