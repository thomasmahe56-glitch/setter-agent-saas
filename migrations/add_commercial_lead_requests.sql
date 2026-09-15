-- Applied to setter-saas-test as 2026091515_add_commercial_lead_requests.
-- This schema is additive and contains only marketing interest/demo requests.
create table public.commercial_lead_requests (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now(),
  intent text not null check (intent in ('interest', 'demo', 'founding')),
  locale text not null check (locale in ('en', 'fr')),
  name text not null check (char_length(name) between 2 and 100),
  email text not null check (char_length(email) between 3 and 200),
  email_normalized text not null check (char_length(email_normalized) between 3 and 200),
  instagram text not null check (char_length(instagram) between 1 and 30),
  offer text not null check (char_length(offer) between 1 and 250),
  need text not null check (char_length(need) between 1 and 600),
  selected_plan text check (selected_plan is null or selected_plan in ('setter', 'prospecting', 'complete')),
  dedupe_key text not null unique check (char_length(dedupe_key) = 64),
  source_ip_hash text not null check (char_length(source_ip_hash) = 64)
);

alter table public.commercial_lead_requests enable row level security;
create index commercial_lead_requests_created_at_idx on public.commercial_lead_requests (created_at desc);
create index commercial_lead_requests_source_ip_created_at_idx on public.commercial_lead_requests (source_ip_hash, created_at desc);
revoke all on table public.commercial_lead_requests from anon, authenticated;
