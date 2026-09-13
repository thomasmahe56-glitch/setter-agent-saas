-- Persistent, tenant-scoped inbound webhook idempotency.
create extension if not exists pgcrypto;

create table if not exists public.processed_inbound_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  channel text not null,
  event_id text not null,
  conversation_id uuid,
  status text not null default 'processing' check (status in ('processing', 'completed', 'failed')),
  last_error text,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  unique (user_id, channel, event_id)
);

create index if not exists processed_inbound_events_user_created_idx
  on public.processed_inbound_events (user_id, created_at desc);

alter table public.processed_inbound_events enable row level security;
revoke all on table public.processed_inbound_events from anon, authenticated;
grant select, insert, update on table public.processed_inbound_events to service_role;

comment on table public.processed_inbound_events is
  'Persistent at-most-once reservation for reliable provider inbound event IDs, scoped by tenant and channel.';
