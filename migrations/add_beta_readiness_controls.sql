-- Nounes beta readiness controls: AI cost tracking/cap.
-- Run in Supabase SQL editor before enabling the cap in production.
-- No secrets are stored here.

create table if not exists public.beta_account_settings (
  user_id uuid primary key references auth.users(id) on delete cascade,
  ai_cost_cap_eur numeric(10,2) not null default 50.00,
  ai_cost_guardrail_enabled boolean not null default true,
  allowed_send_start text not null default '08:00',
  allowed_send_end text not null default '22:00',
  min_auto_delay_seconds integer not null default 0,
  random_auto_delay_seconds integer not null default 0,
  follow_up_config jsonb not null default '[{"stage":"auto_23h","delay_hours":23,"mode":"auto"},{"stage":"j3","delay_hours":72,"mode":"manual"},{"stage":"j10","delay_hours":240,"mode":"manual"},{"stage":"j30","delay_hours":720,"mode":"manual"}]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.beta_ai_usage (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  feature text not null,
  model text not null,
  input_tokens_estimated integer not null default 0,
  output_tokens_estimated integer not null default 0,
  estimated_cost_eur numeric(12,8) not null default 0,
  created_at timestamptz not null default now()
);

create index if not exists beta_ai_usage_user_created_idx on public.beta_ai_usage(user_id, created_at desc);

alter table public.beta_account_settings enable row level security;
alter table public.beta_ai_usage enable row level security;

-- Service-role backend writes/reads these tables. Dashboard access goes through backend JWT endpoints.
