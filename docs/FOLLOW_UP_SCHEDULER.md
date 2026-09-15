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

The Railway backend starts the worker in FastAPI lifespan. It polls every 60
seconds by default. An indexed due-job query and `FOR UPDATE SKIP LOCKED` claim
prevent two workers from receiving the same job. No frontend endpoint, browser,
or local computer is part of the processing path. A timeout or crash after an
external attempt is ambiguous because Meta/ManyChat do not accept an idempotency
key in the current provider adapters. Such a job becomes `manual_required` and
is never retried automatically. This guarantees at-most-once automatic attempts;
an operator must reconcile ambiguous provider outcomes before a manual action.

The channel window is checked before generation and again by the Meta adapter
at send time. The configured delay is never shortened to fit Meta's window.
`manual_required`, `blocked`, `failed`, `sent`, and `cancelled` remain visible
through `/follow-ups/jobs`.

## Rollout

1. On the isolated Supabase project, apply
   `migrations/test_only_follow_up_baseline.sql` and
   `migrations/add_persistent_follow_up_jobs.sql`. Confirm grants, RLS,
   triggers, PostgREST schema cache, and RPC functions using test queries.
2. Deploy both branches to test environments. Keep Railway Serverless disabled
   or otherwise prove the worker stays awake. Confirm the startup worker logs
   and at least one claim from a safe test conversation. Railway's standard
   start command runs one FastAPI instance with the worker in lifespan.
3. Verify Training Center save/reload, future jobs, closed-hours deferral,
   prospect reply cancellation, Meta manual state, a backend restart, and two
   concurrent claim attempts against the test database. Use provider mocks;
   do not contact a real prospect.
4. Review the test evidence with Thomas. The persistent-jobs migration is
   already present on the live database; deploy the backend before dashboard
   to production only after approval. Check queue depth, oldest scheduled
   job, `manual_required` count, logs, and one known-safe tenant. A rollback
   must stop the worker before reverting the application; retain job records
   for reconciliation.

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
The browser demo uses test fixtures, and the local backend uses a dummy Supabase
URL. Real Training Center save/reload, worker queue processing, and Railway
restart behavior remain to be verified end to end before deploying the new
applications to production.

Read-only Railway inspection found that the connected `Angellos` project has
only a `production` environment, sourced from `main`, and no Railway cron
schedule. Its current deployment logs still show dashboard calls to
`/follow-ups/due` with browser-provided delay query parameters.

An isolated Railway project, `angellos-follow-up-test`
(`85d9ac6d-558c-40a4-b0e9-5fcf4d2a8ebb`), was subsequently created with
service `setter-agent-follow-up-test` sourced only from this branch. Its
environment is named `production` by Railway default but is a separate project
from the live `Angellos` project. The service is configured with `/health`,
restart-always, app sleeping disabled, and Meta sending disabled. The cloud
container deployed successfully and logs show `[follow-up] worker_started`.
Its worker was observed starting in cloud logs. Its `SUPABASE_KEY` is a
nonfunctional placeholder. `SUPABASE_URL` now points to
`fagjhniuoopsgmbgdurb` on the separate Railway project. Deployment
`aa5b9994-028a-4458-9b23-9897eafbdebd` succeeded; its logs show
`[workers] disabled by BACKEND_WORKERS_ENABLED`. Do not use credentials
for `lyrlvipkwzbsojqbposh`.

An isolated Supabase project, `angellos-follow-up-test`
(`fagjhniuoopsgmbgdurb`, `eu-west-1`), was created on 2026-09-15. It is a
separate PostgreSQL database on the Free plan. Its scheduler-specific baseline
and persistent-jobs migrations have been applied. All four public test tables
have RLS enabled; service-role grants are present. A synthetic Auth row,
tenant settings row, and conversation were inserted without an email or login
credential. The conversation trigger placed one item in the refresh queue;
there are zero jobs until the backend worker processes it. The synthetic stage
is manual, so it cannot send a provider message. A new project's own secret
key must be configured on the isolated Railway service before cloud worker
verification. The live project's key must never be used there.

Database checks on the isolated project confirmed that the conversation insert
queued one refresh item, a service-role RPC claimed one fictional due job, a
second claim returned zero, and rollback left zero persistent jobs. The
privileged enqueue trigger functions deny direct authenticated execution.
Security advisors reported only informational `rls_enabled_no_policy` notices
for the four deliberately service-role-only test tables.

For `SUPABASE_KEY`, prefer an existing `sb_secret_...` key from a **new,
isolated test** project; it maps to PostgreSQL `service_role` and is sent only
as `apikey`.
The legacy `service_role` JWT also works, with `apikey` and Bearer headers.
Never use the publishable/anon key or the JWT signing secret for this worker.

While the credential is missing, set `BACKEND_WORKERS_ENABLED=false` on this
isolated service to avoid repeated unauthorized database polls. It defaults to
`true` in the backend, and must be restored to `true` once the test credential
is configured, before testing the cloud scheduler.
