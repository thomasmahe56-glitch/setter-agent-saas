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

## Current validation limit

Local automated tests use virtual time and mocked provider/database responses.
The local browser demo uses test fixtures, and the local backend uses a dummy
Supabase URL. A real test Supabase project and test tenant are required to
verify the migration, PostgREST RPCs, persistent saves, and Railway restart
behavior end to end.
