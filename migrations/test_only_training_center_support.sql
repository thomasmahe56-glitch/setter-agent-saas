-- Isolated angellos-follow-up-test project only. Empty support tables let the
-- real Training Center read endpoint return its normal shape during UI tests.
-- Never apply this to an existing Angellos database.
create table public.agent_profiles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users(id) on delete cascade,
  profile jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.agent_avatars (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users(id) on delete cascade,
  source_inputs jsonb not null default '{}'::jsonb,
  avatar jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.agent_sales_rules (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users(id) on delete cascade,
  rules jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.prompt_versions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  created_at timestamptz default now(),
  content text not null,
  is_active boolean default false,
  source text,
  insight_id uuid,
  refinement_instruction text,
  refinement_applied_at timestamptz,
  previous_version_id uuid references public.prompt_versions(id),
  prompt_diff jsonb not null default '[]'::jsonb
);

alter table public.agent_profiles enable row level security;
alter table public.agent_avatars enable row level security;
alter table public.agent_sales_rules enable row level security;
alter table public.prompt_versions enable row level security;
revoke all on public.agent_profiles, public.agent_avatars, public.agent_sales_rules, public.prompt_versions from anon, authenticated;
grant select, insert, update, delete on public.agent_profiles, public.agent_avatars, public.agent_sales_rules, public.prompt_versions to service_role;
