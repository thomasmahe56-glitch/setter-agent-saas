-- Isolated angellos-follow-up-test project only. This deliberately contains
-- the small subset of the live schema exercised by the follow-up scheduler.
-- Never apply this baseline to an existing Angellos database.
create table public.conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  username text not null,
  display_name text,
  external_contact_id text not null,
  channel text default 'instagram' check (channel in ('instagram', 'whatsapp')),
  status text default 'nouveau',
  agent_active boolean default true,
  automation_mode text default 'supervised' check (automation_mode in ('auto', 'supervised', 'disabled')),
  human_takeover boolean not null default false,
  contact_status text not null default 'active' check (contact_status in ('active', 'opted_out')),
  last_inbound_at timestamptz,
  history jsonb default '[]'::jsonb,
  response text,
  created_at timestamptz default now()
);

create table public.beta_account_settings (
  user_id uuid primary key references auth.users(id) on delete cascade,
  allowed_send_start text not null default '08:00',
  allowed_send_end text not null default '22:00',
  follow_up_config jsonb not null default '[]'::jsonb,
  ai_cost_cap_eur numeric not null default 50.00,
  ai_cost_guardrail_enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.conversations enable row level security;
alter table public.beta_account_settings enable row level security;
revoke all on public.conversations, public.beta_account_settings from anon, authenticated;
grant select, insert, update, delete on public.conversations to service_role;
grant select, insert, update, delete on public.beta_account_settings to service_role;
