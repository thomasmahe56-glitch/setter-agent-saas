# Persistent follow-up scheduler

The backend owns the follow-up rules. The Training Center writes `follow_up_config`,
`allowed_send_start`, `allowed_send_end`, and the tenant IANA `timezone` to
`beta_account_settings`. The browser does not supply delays to the scheduler.
Legacy settings without an explicit `enabled: true` are treated as disabled.
New default stages are disabled until the tenant explicitly enables them.
This prevents the migration/backfill from silently starting outreach.
Stages are sequential: only the next enabled stage after the latest sent
assistant message is scheduled. A successful worker send records its
`follow_up_stage` in conversation history, which advances planning to the next
stage. A manual or ambiguous stage does not trigger an automatic later stage.
The worker also rejects higher-stage jobs left by an older backend version,
even if they have already been claimed.

Each sent assistant message or inbound prospect message changes `conversations`.
A database trigger enqueues only that conversation in `follow_up_refresh_queue`.
The backend polls the queue and computes future `follow_up_jobs`. It stores both
`due_at` (the configured elapsed delay) and `scheduled_at` (the first eligible
instant inside the tenant messaging window). UTC instants plus an IANA timezone
preserve correct behavior across daylight-saving transitions.
For an opening time inside the repeated autumn hour, the scheduler chooses the
next real occurrence of that wall time; it can use the second occurrence after
the first has passed. An opening time inside the missing spring hour advances
to the first real local minute.

After a prospect replies, unsent jobs from the previous conversation cycle are
cancelled. When Angellos replies again, a new cycle is scheduled. When a tenant
changes a delay, mode, opening time, or timezone, the settings trigger increments
`follow_up_config_version` and enqueues the tenant's active conversations. Old
unsent jobs are cancelled and new jobs receive a new versioned idempotency key.
The worker also checks the version and conversation state before sending. After
AI generation, it re-reads the latest history, inbound timestamp, tenant version,
takeover/opt-out flags, channel, Meta window, and opening hours before calling
the provider. A reply recorded only in history still cancels the prepared job.
The transport-refresh migration also enqueues a conversation when its provider,
connection, scoped recipient, or opt-out timestamp changes. A tenant switch of
`active_messaging_provider` increments the configuration version and enqueues
its conversations. An inactive tenant Meta provider plans Instagram jobs as
manual and blocks a claimed auto job before any provider call.
Instagram follow-ups require `messaging_provider = meta_instagram`, a Meta
connection ID, and a Meta-scoped recipient ID. Legacy ManyChat conversations
become visible manual jobs; the worker does not send historical subscriber IDs.
A changed provider or recipient during AI generation cancels the prepared send.
An auto tenant stage on a supervised conversation creates a manual job. The
effective mode is part of the idempotency key, so a supervised-to-auto switch
cancels the old job and creates a new auto job without promising an automatic
send while supervision is active.

The Railway backend starts the worker in FastAPI lifespan. It polls every 60
seconds by default. An indexed due-job query and `FOR UPDATE SKIP LOCKED` claim
prevent two workers from receiving the same job. No frontend endpoint, browser,
or local computer is part of the processing path. A timeout or crash after an
external attempt is ambiguous because the Meta adapter does not accept an idempotency
key in the current provider adapters. Such a job becomes `manual_required` and
is never retried automatically. This guarantees at-most-once automatic attempts;
an operator must reconcile ambiguous provider outcomes before a manual action.
Preparation errors before any provider call are different: the worker stores
`next_retry_at`, retries at most three preparations with a bounded backoff,
and exposes the retry time in the dashboard. The original `scheduled_at` stays
intact for audit. An exhausted preparation becomes visible as `failed`. A
worker crash while `processing` is still treated conservatively as ambiguous
because the database cannot know whether an external call had started.

The channel window is checked when planning, before generation, after
generation, and again by the Meta adapter at send time. If the planned instant already lies beyond the
Meta window, the future job is labelled manual and keeps the channel reason;
it becomes `manual_required` only at its configured time. The configured delay
is never shortened to fit Meta's window.
`META_INSTAGRAM_REPLY_WINDOW_HOURS` may make the automatic window stricter,
but is capped at 24 hours in both server configuration and the Meta send gate.
The human-agent exception is not used for automatic follow-ups.
For follow-ups, the Meta adapter makes one HTTP attempt and requires a 2xx
response with a nonempty `message_id` and, when present, the expected
`recipient_id`. A 2xx response without this delivery receipt is ambiguous:
the job becomes `manual_required`, no sent usage is recorded, and a human must
compare the Meta thread before retrying. Meta's published [Instagram Send API
example](https://www.postman.com/meta/instagram/request/1rgmhuk/text-message)
shows both receipt fields.
`manual_required`, `blocked`, `failed`, `sent`, and `cancelled` remain visible
through `/follow-ups/jobs`.
The existing `/usage/summary` field `follow_ups` counts AI generations
(`follow_up_generated`), including drafts that never reached a provider. It is
not a delivery counter. The Follow-ups page's **Sent** group reads
`follow_up_jobs.status = sent`, which the worker writes only after a successful
provider response. No separate `auto_sent` field exists in these repositories.
AI usage uses a distinct idempotency key per preparation attempt so a retry
that generates a second draft is counted separately. Provider delivery still
uses the single job claim and never automatically retries an ambiguous send.
If the provider accepted a message but usage-ledger recording fails, the
adapter logs that accounting failure and still returns the successful delivery
response so the job becomes `sent` instead of an ambiguous retry.
The ManyChat adapter also rejects a JSON body declaring `status: error` (or an
`error` object) even if a gateway returned HTTP 200. Such a response does not
increment sent usage or mark the job `sent`; code 3011 requires manual action.
This is legacy compatibility, not the active Instagram sending path.
After Meta accepts a follow-up, the worker calls the service-role-only
`append_sent_follow_up_history` RPC. It appends the sent message once by job ID,
even if a prospect replied between delivery and history persistence. It updates
the conversation's latest `response` and `status` only when history and the
inbound timestamp still match the pre-send snapshot.
This idempotent database-only sync may retry three times without repeating the
provider send. If it still fails, the job remains `sent` with a visible
reconciliation error; an operator must compare the Meta thread with CRM history.

## Rollout

1. On the isolated Supabase project, apply
   `migrations/test_only_follow_up_baseline.sql` and
   `migrations/add_persistent_follow_up_jobs.sql`, followed by
   `migrations/add_follow_up_preparation_retry.sql` and
   `migrations/append_sent_follow_up_history.sql`, then
   `migrations/refresh_follow_up_transport_changes.sql`. Confirm grants, RLS,
   triggers, PostgREST schema cache, and RPC functions using test queries.
2. Deploy the backend branch to the isolated Railway project and point a local
   dashboard at that service and the isolated Supabase database. Keep Railway
   Serverless disabled or otherwise prove the worker stays awake. Confirm the
   startup worker logs and at least one claim from a safe test conversation.
   Railway's standard start command runs one FastAPI instance with the worker
   in lifespan.
3. Verify Training Center save/reload, future jobs, closed-hours deferral,
   prospect reply cancellation, Meta manual state, a backend restart, and two
   concurrent claim attempts against the test database. Use provider mocks;
   do not contact a real prospect.
4. Review the test evidence with Thomas. The persistent-jobs migration is
   already present on the live database; apply the additive preparation-retry,
   atomic-history, and transport-refresh migrations, then deploy the backend
   before the dashboard
   to production only after approval. The old dashboard still calls
   `/follow-ups/due` with browser-supplied delays. During the short interval
   before the dashboard deploy, that legacy route returns an empty list rather
   than an error; no browser rule can schedule or send a job. Deploy the new
   dashboard promptly to expose the server jobs. Check queue depth, oldest
   scheduled job, `manual_required` count, logs, and one known-safe tenant.
   A rollback must stop the worker before reverting the application; retain
   job records for reconciliation.

The active database currently has one tenant settings row whose stages still
use `auto_23h`, `j3`, `j10`, and `j30` without an explicit `enabled: true`.
The new normalizer maps them to generic stages but keeps all four disabled.
The 92 queued existing conversations therefore cannot begin automatic outreach
merely because the worker is deployed. After the approved backend/dashboard
rollout, explicitly save the desired enabled stages (for example +7 hours) in
the live Training Center and verify the resulting job for a known-safe Meta
conversation. That conversation's inbound timestamp must also be within the
allowed Meta window before an automatic send can occur; a newly configured
delay never overrides the channel gate.

## Live-project migration incident and validation (2026-09-15)

The migration was applied to the Supabase project named `setter-saas-test`
(`lyrlvipkwzbsojqbposh`) after Thomas approved that named project. Further
workspace inspection showed that its project ref is also used by the deployed
dashboard: despite its name, this is **not an isolated test database**. No
Supabase project was renamed or deleted. The live backend and dashboard were
not redeployed.

The schema, three triggers, service-role-only RPC grants, and RLS/grants were
verified. The backfill placed 92 active conversations in the refresh queue;
there are zero follow-up jobs and zero settings with explicitly enabled stages.
Most legacy conversations have no tenant follow-up settings, so the strict
server reader uses disabled default stages rather than starting outreach.
The refresh worker processes at most 50 queued rows per poll. A malformed
historical conversation is logged and left in the queue for retry while later
rows in the same batch continue; it cannot starve the current Meta tenant.
In a rolled-back SQL transaction, a fictional due job was claimed once and a
second claim returned zero. A rolled-back tenant timezone update incremented
the config version. After both probes, the project still had zero jobs, 92
queued conversations, and the original tenant settings version.

The initial trigger functions used invoker privileges while the queue denied
`authenticated` access. A rolled-back update of one owned conversation as an
authenticated user reproduced `permission denied for table
follow_up_refresh_queue`; this could have blocked live conversation writes.
The corrective migration `fix_follow_up_refresh_trigger_permissions.sql` was
applied immediately. Its three trigger functions now run as their `postgres`
owner with an empty search path, and client roles still cannot execute them
directly or access the queue. The same rolled-back authenticated update then
succeeded. Live counts remained 92 conversations, 887 prospects, one settings
row, and zero follow-up jobs.

Local automated tests use virtual time and mocked provider/database responses.
The successful-delivery test follows a due job through one provider attempt,
`sent` persistence, and a `follow_up_job_id` entry in conversation history;
timeout and competing-worker tests cover the opposite outcomes. No test
provider sends to a real account.
An integrated worker test also runs the refresh queue, creates a future job,
advances a virtual clock to its due time, claims it, calls a simulated provider
once, persists `sent` and the conversation history, and confirms that another
worker pass cannot send it again. Its database and provider adapters are fake;
the isolated Railway/Supabase checks below separately prove cloud persistence
and real queue/RPC operation without outbound messaging.
The integrated test also simulates a first AI-preparation outage: the job stays
scheduled with a persisted retry instant, cannot be claimed early, and is sent
once after the virtual clock advances. On the isolated Supabase project, the
additive retry migration was applied, then a transaction-local synthetic job
proved the real RPC skips a future retry, claims it once when eligible, and
cannot claim it a second time. The synthetic row was removed in that same
transaction, and a follow-up query found zero test-RPC rows.
The initial browser demo used test fixtures and the initial local backend used
a dummy Supabase URL. The later isolated Railway/Supabase setup verified real
Training Center save/reload, worker queue processing, and Railway redeployment
with synthetic records, as described below. Provider delivery remains disabled
in that setup, so production rollout still requires approval and monitoring.

Read-only Railway inspection found that the connected `Angellos` project has
only a `production` environment, sourced from `main`, and no Railway cron
schedule. Its current deployment logs still show dashboard calls to
`/follow-ups/due` with browser-provided delay query parameters.
The production service `setter-agent-saas` is still on a successful deployment
from 2026-09-13. Its Railway configuration specifies one replica in `us-west2`
and has no `BACKEND_WORKERS_ENABLED` or `SCHEDULED_REPLY_WORKER_ENABLED`
override; after the new backend is deployed, both workers use their default
enabled state. `SUPABASE_URL` and `SUPABASE_KEY` variable names are present,
but Railway's connected OAuth view withholds their values. The isolated test
credential must not replace either production variable.

An isolated Railway project, `angellos-follow-up-test`
(`85d9ac6d-558c-40a4-b0e9-5fcf4d2a8ebb`), was subsequently created with
service `setter-agent-follow-up-test` sourced only from this branch. Its
environment is named `production` by Railway default but is a separate project
from the live `Angellos` project. The service is configured with `/health`,
restart-always, app sleeping disabled, and Meta sending disabled. The cloud
container deployed successfully and logs show `[follow-up] worker_started`.
`SUPABASE_URL` and the server-only `SUPABASE_KEY` belong to the isolated
Supabase project `fagjhniuoopsgmbgdurb`. `BACKEND_WORKERS_ENABLED=true`,
`SCHEDULED_REPLY_WORKER_ENABLED=false`, and the test Auth user is the only
`ALLOWED_USER_IDS` entry. Its public test API domain is
`https://setter-agent-follow-up-test-production.up.railway.app`; the local
dashboard proxy uses a separate test-only `DASHBOARD_SECRET`. Do not use
credentials for `lyrlvipkwzbsojqbposh` here.

An isolated Supabase project, `angellos-follow-up-test`
(`fagjhniuoopsgmbgdurb`, `eu-west-1`), was created on 2026-09-15. It is a
separate PostgreSQL database on the Free plan. Its scheduler-specific baseline
and persistent-jobs migrations have been applied. Test-only support migrations
add the empty tables needed by Training Center and `prompt-versions`; all
public test tables have RLS enabled and service-role grants. A synthetic Auth
user was made login-capable with a non-deliverable `.invalid` email address
and a test-only password that is not committed. All conversations are synthetic.
The test-only `messaging_provider` support migration routes synthetic auto
jobs to the Meta adapter, whose outbound switch is disabled on this service.
The live project's key must never be used here.

With the isolated credential configured, the cloud follow-up worker processed
the one synthetic queue item and created one scheduled manual job. The same
container's unrelated scheduled-reply worker logged HTTP errors because this
test-only database intentionally lacks its tables. Set
`SCHEDULED_REPLY_WORKER_ENABLED=false` on the isolated Railway service; the
backend now supports this switch while retaining its default enabled state
for existing installations.

Cloud integration checks with only synthetic conversations then showed:

- a tenant-configured manual stage created a future `scheduled` job;
- a later synthetic inbound reply changed that job to `cancelled` and emptied
  the refresh queue;
- an auto stage whose Instagram inbound was 48 hours old changed to
  `manual_required` with `instagram/Meta automatic messaging window closed`;
- an opening window of 23:00-23:01 in `Europe/Paris` deferred a synthetic
  due job to 21:00 UTC, with `scheduled_at > due_at`;
- Railway redeployment `20b59b8f-a78d-44ea-8781-e805724c7f75` succeeded;
  the job states persisted and logs showed only the follow-up worker starting.

The separate test service still has Meta outbound disabled. The isolated
database has no real prospects. The local dashboard at `localhost:3002` used
an ignored `.env.local` that points only to the isolated Supabase project and
Railway test API. Browser verification completed normal login, Training Center
save/reload for `follow_up_1 = 7 hours`, `08:00-20:00`, `Europe/Paris`, and a
direct SQL read confirmed these values persisted in `beta_account_settings`
with config version 4. Changing the hours to `23:00-23:01` incremented the
version to 5; the backend cancelled prior scheduled jobs and created new jobs
with `due_at` around 19:33 Paris and `scheduled_at` 23:00 Paris. The Follow-ups
page showed scheduled future jobs, the closed-hours explanation, Meta manual
state, and cancelled jobs. The initial browser overlay was caused by the
minimal test schema lacking `prompt_versions`; after adding that empty support
table and reloading, Training Center and Follow-ups showed no error overlay.
Real browser screenshots were inspected inline; the earlier demo screenshots
are stored in the dashboard branch for review.

A further isolated browser check changed the same tenant to an enabled
`follow_up_1` in **auto** mode at `+7 hours`, with `08:00-20:00` and
`Europe/Paris`. SQL confirmed configuration version 6. The cloud worker
replaced the earlier manual jobs with a synthetic future auto job due at
19:33 Paris. For a second synthetic conversation whose Instagram inbound was
already 48 hours old, it cancelled the stale auto job and created a future
manual job due at 19:32 Paris, retaining the Meta-window reason. The Follow-ups
page showed the auto job before its due time and explained that the Meta-limited
job needs manual action **after** its due time. No provider connection or real
prospect exists in this test database, and outbound Meta is disabled.

After the sequential-stage change, the isolated browser added and saved
`follow_up_2` as enabled/auto at `+1 hour` while `follow_up_1` remained
enabled/auto at `+7 hours`. SQL read configuration version 7 and both stages.
The new Railway worker processed the refresh queue and persisted only two
future `follow_up_1` jobs, one for each eligible synthetic conversation;
there were zero queued conversations and no active `follow_up_2` job. The
Follow-ups page showed only the two stage-1 jobs, and browser console errors
were empty. This verifies that a shorter later-stage delay cannot overtake
the first stage. Both test conversations use a disabled outbound Meta adapter.

Database checks on the isolated project confirmed that the conversation insert
queued one refresh item, a service-role RPC claimed one fictional due job, a
second claim returned zero, and rollback left zero persistent jobs. The
privileged enqueue trigger functions deny direct authenticated execution.
Security advisors reported only informational `rls_enabled_no_policy` notices
for the four deliberately service-role-only test tables.

For `SUPABASE_KEY`, prefer an existing `sb_secret_...` key from an **isolated
test** project; it maps to PostgreSQL `service_role` and is sent only
as `apikey`.
The legacy `service_role` JWT also works, with `apikey` and Bearer headers.
Never use the publishable/anon key or the JWT signing secret for this worker.

The workers were paused while the credential was missing; they are now enabled
on the isolated Railway service. The unrelated scheduled-reply worker remains
disabled there because the scheduler-specific test database lacks its tables.

After the worker began requiring a Meta connection selector and scoped recipient,
the minimal isolated schema needed `test_only_meta_delivery_identity.sql`. It
adds the production-shaped selector column and a synthetic placeholder UUID to
the two disposable auto conversations. This placeholder is not a real Meta
connection; the test service has Meta sending disabled. The production schema
already has the column and its foreign key.

With config version 7 still at `+7h`, `08:00-20:00`, `Europe/Paris`, the
isolated Synthetic Closed Hours conversation was given a synthetic sent message
at 14:00 Paris. The cloud worker consumed its refresh item, cancelled the
previous scheduled job, and created exactly one stage-1 auto job with
`due_at = 21:00` Paris and `scheduled_at = 08:00` Paris the next day. A real
browser reload showed both times and “messagerie fermée à l’échéance”; the
Follow-ups request returned HTTP 200. A synthetic prospect reply was then
recorded on the other scheduled conversation. The cloud worker consumed that
refresh item, cancelled its future job, and the browser's scheduled count fell
from two to one; the closed-hours job stayed visible. Browser console had no
errors, only Next.js smooth-scroll warnings. No DM was sent in either test.

Meta follow-up receipt verification was tested with mocked HTTP 200 responses:
only a matching `message_id` receipt counts as sent; missing, malformed, or
wrong-recipient receipts produce `meta.message.unverified` and never count sent
usage. A disposable `manual_required` row with this error was rendered in the
real local Follow-ups page. The card explained in French that the Instagram
thread must be checked before another send, browser console errors were zero,
and the synthetic row was deleted from the isolated database afterward.

The virtual-time end-to-end test now traverses the queue refresh, persisted
job claim, AI preparation, channel router, Meta eligibility gate, mocked Meta
delivery receipt, usage ledger, sent-job transition, and idempotent CRM history
append. It asserts the adapter receives the scoped recipient and `max_attempts=1`,
including after a simulated AI preparation retry. Stage 2 remains absent until
stage 1 is sent and appended to CRM history; a subsequent queue refresh then
schedules it at its own +1 hour delay and sends it once with a distinct Meta
receipt. This is a local mock of the provider response; it does not send a real
Instagram DM.

The transport-refresh migration was applied only to the isolated test project
after checking that its three synthetic conversations and project ref differ
from the live project with 92 conversations. Transactional probes changed a
synthetic connection ID and the tenant's active provider: the first enqueued
that conversation, and the second incremented config version and enqueued all
three tenant conversations. Both probes were rolled back; version 7, active
Meta provider, and an empty refresh queue were confirmed afterward.
The worker tests also covered an inactive tenant Meta provider and an
`opted_out_at` timestamp without a matching contact-status update. A disposable
inactive-provider job rendered a French connection-repair explanation on the
real Follow-ups page; browser console errors were empty, and the row was
deleted from the isolated database afterward.

A final visual check inserted a single test-only `sent` job for the synthetic
`Synthetic Follow Up` conversation, without contacting Meta. A real browser
reload showed **Envoyées (1)** and a French card with the theoretical due time,
planned time, sent time, and `Europe/Paris` timezone; the layout had no overlay
and browser console errors were empty. The exact disposable row was deleted
after inspection, and a second reload showed **Envoyées (0)** again. This
verifies sent-state rendering, not a real provider delivery.

The **À échéance / en cours** group was checked separately with one disposable
synthetic `processing` job in the same isolated database. A browser reload
showed **À échéance / en cours (1)**, the **En cours** badge, its due time, and
the tenant timezone. The card was visually inspected, browser console errors
were empty, and the exact synthetic row was removed. A further reload showed
the group back at zero. This verifies processing-state presentation without a
provider call.
