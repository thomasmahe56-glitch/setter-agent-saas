-- Repairs the isolated test project's already-applied baseline. A fresh test
-- project has these columns directly in test_only_follow_up_baseline.sql.
alter table public.beta_account_settings
  add column if not exists min_auto_delay_seconds integer not null default 0;
alter table public.beta_account_settings
  add column if not exists random_auto_delay_seconds integer not null default 0;
