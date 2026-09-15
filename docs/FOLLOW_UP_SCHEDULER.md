# Persistent follow-up scheduler

The backend owns the follow-up rules. The Training Center writes `follow_up_config`,
`allowed_send_start`, `allowed_send_end`, and the tenant IANA `timezone` to
`beta_account_settings`. The browser does not supply delays to the scheduler.
Legacy settings without an explicit `enabled: true` are treated as disabled.
New default stages are disabled until the tenant explicitly enables them.
This prevents the migration/backfill from silently starting outreach.

Each sent assistant message or inbound prospect message changes `conversations`.
A database trigger enqueues only that conversation in `follow_up_refresh_queue`.
The backend polls the queue and computes future `follow_up_jobs`. It stores both
`due_at` (the configured elapsed delay) and `scheduled_at` (the first eligible
instant inside the tenant messaging window). UTC instants plus an IANA timezone
preserve correct behavior across daylight-saving transitions.

After a prospect replies, unsent jobs from the previous conversation cycle are
cancelled. When Angellos replies again, a new cycle is scheduled. When a tenant
changes a delay, mode, opening time, or timezone, the settings trigger increments
`follow_up_config_version` and enqueues the tenant's active conversations. Old
unsent jobs are cancelled and new jobs receive a new versioned idempotency key.
The worker also checks the version and latest inbound timestamp before sending.
An auto tenant stage on a supervised conversation creates a manual job. The
effective mode is part of the idempotency key, so a supervised-to-auto switch
cancels the old job and creates a new auto job without promising an automatic
send while supervision is active.

The Railway backend starts the worker in FastAPI lifespan. It polls every 60
seconds by default. An indexed due-job query and `FOR UPDATE SKIP LOCKED` claim
prevent two workers from receiving the same job. No frontend endpoint, browser,
or local computer is part of the processing path. A timeout or crash after an
external attempt is ambiguous because Meta/ManyChat do not accept an idempotency
key in the current provider adapters. Such a job becomes `manual_required` and
is never retried automatically. This guarantees at-most-once automatic attempts;
an operator must reconcile ambiguous provider outcomes before a manual action.

The channel window is checked when planning, before generation, and again by
the Meta adapter at send time. If the planned instant already lies beyond the
Meta window, the future job is labelled manual and keeps the channel reason;
it becomes `manual_required` only at its configured time. The configured delay
is never shortened to fit Meta's window.
`manual_required`, `blocked`, `failed`, `sent`, and `cancelled` remain visible
through `/follow-ups/jobs`.

## Rollout

1. On the isolated Supabase project, apply
   `migrations/test_only_follow_up_baseline.sql` and
   `migrations/add_persistent_follow_up_jobs.sql`. Confirm grants, RLS,
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
   already present on the live database; deploy the backend before dashboard
   to production only after approval. The old dashboard still calls
   `/follow-ups/due` with browser-supplied delays. During the short interval
   before the dashboard deploy, that legacy route returns an empty list rather
   than an error; no browser rule can schedule or send a job. Deploy the new
   dashboard promptly to expose the server jobs. Check queue depth, oldest
   scheduled job, `manual_required` count, logs, and one known-safe tenant.
   A rollback must stop the worker before reverting the application; retain
   job records for reconciliation.

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
