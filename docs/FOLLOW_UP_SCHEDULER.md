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

1. Apply `migrations/add_persistent_follow_up_jobs.sql` to a **test** Supabase
   project. Confirm table grants, RLS, trigger creation, PostgREST schema cache,
   and the RPC functions using test queries.
2. Deploy both branches to test environments. Keep Railway Serverless disabled
   or otherwise prove the worker stays awake. Confirm the startup worker logs
   and at least one claim from a safe test conversation. Railway's standard
   start command runs one FastAPI instance with the worker in lifespan.
3. Verify Training Center save/reload, future jobs, closed-hours deferral,
   prospect reply cancellation, Meta manual state, a backend restart, and two
   concurrent claim attempts against the test database. Use provider mocks;
   do not contact a real prospect.
4. Review the test evidence with Thomas. Then apply the migration and deploy
   backend before dashboard to production. Check queue depth, oldest scheduled
   job, `manual_required` count, logs, and one known-safe tenant. A rollback
   must stop the worker before reverting the application; retain job records
   for reconciliation.

## Test-project validation (2026-09-15)

The migration was applied to `setter-saas-test` (`lyrlvipkwzbsojqbposh`).
The schema, three triggers, service-role-only RPC grants, and RLS/grants were
verified. The backfill placed 92 active conversations in the refresh queue;
there are zero follow-up jobs and zero settings with explicitly enabled stages.
In a rolled-back SQL transaction, a fictional due job was claimed once and a
second claim returned zero. A rolled-back tenant timezone update incremented
the config version. After both probes, the project still had zero jobs, 92
queued conversations, and the original tenant settings version.

Local automated tests use virtual time and mocked provider/database responses.
The browser demo uses test fixtures, and the local backend uses a dummy Supabase
URL. PostgREST access with the test backend credentials, real Training Center
save/reload, worker queue processing, and Railway restart behavior remain to
be verified end to end before a production rollout.

Read-only Railway inspection found that the connected `Angellos` project has
only a `production` environment, sourced from `main`, and no Railway cron
schedule. Its current deployment logs still show dashboard calls to
`/follow-ups/due` with browser-provided delay query parameters. There is no
isolated Railway test environment available for the new backend yet.

An isolated Railway project, `angellos-follow-up-test`
(`85d9ac6d-558c-40a4-b0e9-5fcf4d2a8ebb`), was subsequently created with
service `setter-agent-follow-up-test` sourced only from this branch. Its
environment is named `production` by Railway default but is a separate project
from the live `Angellos` project. The service is configured with `/health`,
restart-always, app sleeping disabled, and Meta sending disabled. The cloud
container deployed successfully and logs show `[follow-up] worker_started`.
Its `SUPABASE_KEY` is intentionally a nonfunctional placeholder, so the worker
currently logs an authorization failure and cannot process the 92 queued
conversations. The test database still contains zero jobs and zero enabled
stages. A test-project service-role credential must be configured securely
before end-to-end processing can be checked.

For `SUPABASE_KEY`, prefer an existing `sb_secret_...` key from the **test**
project; it maps to PostgreSQL `service_role` and is sent only as `apikey`.
The legacy `service_role` JWT also works, with `apikey` and Bearer headers.
Never use the publishable/anon key or the JWT signing secret for this worker.

While the credential is missing, set `BACKEND_WORKERS_ENABLED=false` on this
isolated service to avoid repeated unauthorized database polls. It defaults to
`true` in the backend, and must be restored to `true` once the test credential
is configured, before testing the cloud scheduler.
