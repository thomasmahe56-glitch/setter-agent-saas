-- Provider-neutral messaging connections for ManyChat -> Meta Native migration.
create extension if not exists pgcrypto;

create table if not exists public.messaging_connections (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null check (provider in ('manychat', 'meta_instagram')),
  status text not null default 'connected'
    check (status in ('connected', 'needs_reauthorization', 'expired', 'revoked', 'disconnected', 'error')),
  external_account_id text not null,
  external_username text,
  scopes text[] not null default '{}',
  access_token_encrypted text,
  token_expires_at timestamptz,
  connected_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  disconnected_at timestamptz,
  last_webhook_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  unique (provider, external_account_id),
  unique (user_id, provider)
);

create index if not exists messaging_connections_user_status_idx
  on public.messaging_connections (user_id, provider, status);

alter table public.messaging_connections enable row level security;
revoke all on table public.messaging_connections from anon, authenticated;
grant select, insert, update, delete on table public.messaging_connections to service_role;

create table if not exists public.messaging_oauth_states (
  id uuid primary key default gen_random_uuid(),
  state_hash text not null unique,
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null check (provider = 'meta_instagram'),
  code_verifier_encrypted text,
  expires_at timestamptz not null,
  consumed_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists messaging_oauth_states_expiry_idx
  on public.messaging_oauth_states (expires_at);

alter table public.messaging_oauth_states enable row level security;
revoke all on table public.messaging_oauth_states from anon, authenticated;
grant select, insert, update, delete on table public.messaging_oauth_states to service_role;

alter table public.conversations
  add column if not exists messaging_provider text not null default 'manychat'
    check (messaging_provider in ('manychat', 'meta_instagram', 'meta_whatsapp_cloud_api')),
  add column if not exists messaging_connection_id uuid references public.messaging_connections(id) on delete set null,
  add column if not exists human_takeover boolean not null default false,
  add column if not exists contact_status text not null default 'active'
    check (contact_status in ('active', 'opted_out')),
  add column if not exists opted_out_at timestamptz;

create index if not exists conversations_connection_external_idx
  on public.conversations (messaging_connection_id, external_contact_id);

drop index if exists public.conversations_user_channel_external_idx;
create unique index conversations_user_channel_provider_external_idx
  on public.conversations (user_id, channel, messaging_provider, external_contact_id);

alter table public.processed_inbound_events
  add column if not exists provider text,
  add column if not exists messaging_connection_id uuid references public.messaging_connections(id) on delete set null;

update public.processed_inbound_events
set provider = case when channel = 'whatsapp' then 'meta_whatsapp_cloud_api' else 'manychat' end
where provider is null;

alter table public.processed_inbound_events alter column provider set not null;

alter table public.beta_account_settings
  add column if not exists active_messaging_provider text not null default 'manychat'
    check (active_messaging_provider in ('manychat', 'meta_instagram'));

create unique index if not exists processed_inbound_events_tenant_provider_connection_event_idx
  on public.processed_inbound_events (user_id, provider, messaging_connection_id, event_id) nulls not distinct;

comment on column public.messaging_connections.access_token_encrypted is
  'Versioned AES-256-GCM envelope. Never expose through a client-facing API or RLS policy.';
comment on column public.conversations.messaging_provider is
  'Explicit outbound provider. Prevents ManyChat and Meta Native from both automating one conversation.';
