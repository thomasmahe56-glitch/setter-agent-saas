-- Repairs the already-applied isolated Training Center support migration.
-- A fresh test project gets this table in test_only_training_center_support.sql.
create table if not exists public.prompt_versions (
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
alter table public.prompt_versions enable row level security;
revoke all on public.prompt_versions from anon, authenticated;
grant select, insert, update, delete on public.prompt_versions to service_role;
