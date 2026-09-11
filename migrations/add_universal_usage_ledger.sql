-- Universal Angellos variable-usage ledger.
-- Safe to run repeatedly. Existing beta_ai_usage history is preserved and backfilled.

create extension if not exists pgcrypto;

alter table public.beta_ai_usage add column if not exists pricing_snapshot jsonb not null default '{}'::jsonb;
create unique index if not exists beta_ai_usage_user_idempotency_uidx
  on public.beta_ai_usage (user_id, idempotency_key);

create table if not exists public.usage_ledger (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  module text not null,
  feature text not null,
  event_type text not null,
  provider text,
  service text,
  model text,
  provider_event_id text,
  quantity numeric(20,6) not null default 1 check (quantity >= 0),
  unit text not null default 'operation',
  input_tokens bigint check (input_tokens is null or input_tokens >= 0),
  output_tokens bigint check (output_tokens is null or output_tokens >= 0),
  cache_creation_input_tokens bigint check (cache_creation_input_tokens is null or cache_creation_input_tokens >= 0),
  cache_read_input_tokens bigint check (cache_read_input_tokens is null or cache_read_input_tokens >= 0),
  reasoning_tokens bigint check (reasoning_tokens is null or reasoning_tokens >= 0),
  provider_cost numeric(20,8),
  provider_currency text,
  exchange_rate_to_eur numeric(20,10),
  cost_eur numeric(20,8),
  cost_accuracy text not null check (cost_accuracy in ('provider_reported', 'provider_usage_priced', 'estimated', 'unknown')),
  cost_source text not null,
  pricing_version text,
  pricing_snapshot jsonb not null default '{}'::jsonb,
  conversation_id uuid,
  prospect_id uuid,
  campaign_id uuid,
  run_id uuid,
  request_kind text,
  status text not null default 'recorded',
  idempotency_key text not null,
  retry_group_id text,
  metadata jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  constraint usage_ledger_cost_consistency check (
    (cost_accuracy = 'unknown' and cost_eur is null)
    or (cost_accuracy <> 'unknown' and cost_eur is not null)
  )
);

create unique index if not exists usage_ledger_user_idempotency_uidx
  on public.usage_ledger (user_id, idempotency_key);
create unique index if not exists usage_ledger_provider_event_uidx
  on public.usage_ledger (user_id, provider, provider_event_id, event_type)
  where provider_event_id is not null;
create index if not exists usage_ledger_user_occurred_idx
  on public.usage_ledger (user_id, occurred_at desc);
create index if not exists usage_ledger_user_module_occurred_idx
  on public.usage_ledger (user_id, module, occurred_at desc);
create index if not exists usage_ledger_user_feature_occurred_idx
  on public.usage_ledger (user_id, feature, occurred_at desc);
create index if not exists usage_ledger_cost_accuracy_occurred_idx
  on public.usage_ledger (cost_accuracy, occurred_at desc);
create index if not exists usage_ledger_conversation_idx
  on public.usage_ledger (conversation_id) where conversation_id is not null;
create index if not exists usage_ledger_prospect_idx
  on public.usage_ledger (prospect_id) where prospect_id is not null;
create index if not exists usage_ledger_campaign_idx
  on public.usage_ledger (campaign_id) where campaign_id is not null;
create index if not exists usage_ledger_run_idx
  on public.usage_ledger (run_id) where run_id is not null;

alter table public.usage_ledger enable row level security;
grant all on table public.usage_ledger to service_role;
revoke all on table public.usage_ledger from anon;
revoke all on table public.usage_ledger from authenticated;

drop policy if exists usage_ledger_owner_select on public.usage_ledger;
-- Raw provider costs are internal COGS. Authenticated clients receive only the
-- server-filtered /usage/summary contract, never direct ledger access.

create table if not exists public.credit_rules (
  id uuid primary key default gen_random_uuid(),
  module text not null,
  event_type text not null,
  credits_per_unit numeric(20,6) not null check (credits_per_unit >= 0),
  version text not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  enabled boolean not null default false,
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_to > effective_from),
  unique (module, event_type, version)
);

create table if not exists public.credit_transactions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  usage_ledger_id uuid references public.usage_ledger(id) on delete restrict,
  transaction_type text not null check (transaction_type in ('monthly_grant', 'purchase', 'usage_debit', 'manual_adjustment', 'refund')),
  credits numeric(20,6) not null check (credits <> 0),
  rule_version text,
  idempotency_key text not null,
  metadata jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (user_id, idempotency_key),
  unique (usage_ledger_id)
);

create index if not exists credit_transactions_user_occurred_idx
  on public.credit_transactions (user_id, occurred_at desc);
alter table public.credit_rules enable row level security;
alter table public.credit_transactions enable row level security;
grant all on table public.credit_rules to service_role;
grant all on table public.credit_transactions to service_role;
revoke all on table public.credit_rules from anon, authenticated;
revoke all on table public.credit_transactions from anon;
revoke insert, update, delete on table public.credit_transactions from authenticated;
grant select on table public.credit_transactions to authenticated;
drop policy if exists credit_transactions_owner_select on public.credit_transactions;
create policy credit_transactions_owner_select
  on public.credit_transactions for select
  to authenticated
  using ((select auth.uid()) = user_id);

create or replace function public.apply_credit_rule_to_usage()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  matched_rule public.credit_rules%rowtype;
  debit numeric(20,6);
begin
  select * into matched_rule
  from public.credit_rules r
  where r.enabled = true
    and r.module = new.module
    and r.event_type = new.event_type
    and r.effective_from <= new.occurred_at
    and (r.effective_to is null or new.occurred_at < r.effective_to)
  order by r.effective_from desc, r.created_at desc
  limit 1;

  if matched_rule.id is null then
    return new;
  end if;
  debit := round(-(new.quantity * matched_rule.credits_per_unit), 6);
  if debit = 0 then
    return new;
  end if;
  insert into public.credit_transactions (
    user_id, usage_ledger_id, transaction_type, credits,
    rule_version, idempotency_key, metadata, occurred_at
  ) values (
    new.user_id, new.id, 'usage_debit', debit,
    matched_rule.version, 'usage_debit:' || new.id::text || ':' || matched_rule.version,
    jsonb_build_object('credit_rule_id', matched_rule.id), new.occurred_at
  ) on conflict do nothing;
  return new;
end;
$$;

revoke execute on function public.apply_credit_rule_to_usage() from public, anon, authenticated;

drop trigger if exists usage_ledger_apply_credit_rule on public.usage_ledger;
create trigger usage_ledger_apply_credit_rule
after insert on public.usage_ledger
for each row execute function public.apply_credit_rule_to_usage();

-- Existing beta rows remain the compatibility source for /beta/ai-cost during rollout.
insert into public.usage_ledger (
  user_id, module, feature, event_type, provider, service, model,
  provider_event_id, quantity, unit, input_tokens, output_tokens,
  cache_creation_input_tokens, cache_read_input_tokens, reasoning_tokens,
  provider_cost, provider_currency, exchange_rate_to_eur, cost_eur,
  cost_accuracy, cost_source, pricing_version, pricing_snapshot,
  conversation_id, request_kind, status, idempotency_key, metadata,
  occurred_at, created_at
)
select
  b.user_id,
  case when b.feature like 'agent_%' or b.feature like 'training_%' then 'training' else 'setter' end,
  b.feature,
  case
    when coalesce(b.request_kind, b.feature) like '%follow_up%' then 'follow_up_generated'
    when coalesce(b.request_kind, b.feature) in ('inbound_reply', 'activation_supervised', 'playground') then 'assistant_reply_generated'
    else 'ai_generation'
  end,
  coalesce(b.provider, 'anthropic'), 'llm', b.model, b.provider_response_id,
  1, 'generation', b.input_tokens, b.output_tokens,
  b.cache_creation_input_tokens, b.cache_read_input_tokens, b.reasoning_tokens,
  b.provider_cost, b.provider_currency, b.exchange_rate_to_eur,
  coalesce(b.cost_eur, b.estimated_cost_eur),
  case
    when coalesce(b.cost_eur, b.estimated_cost_eur) is null then 'unknown'
    when b.cost_source = 'provider_reported' then 'provider_reported'
    when b.cost_eur is not null and b.input_tokens is not null then 'provider_usage_priced'
    else 'estimated'
  end,
  coalesce(b.cost_source, 'beta_ai_usage_backfill'), b.pricing_version, coalesce(b.pricing_snapshot, '{}'::jsonb),
  b.conversation_id, coalesce(b.request_kind, b.feature), coalesce(b.status, 'recorded'),
  'beta_ai_usage:' || b.id::text,
  jsonb_build_object('legacy_table', 'beta_ai_usage', 'legacy_id', b.id),
  b.created_at, b.created_at
from public.beta_ai_usage b
on conflict do nothing;

create or replace function public.mirror_beta_ai_usage_to_ledger()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  insert into public.usage_ledger (
    user_id, module, feature, event_type, provider, service, model,
    provider_event_id, input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens, reasoning_tokens,
    provider_cost, provider_currency, exchange_rate_to_eur, cost_eur,
    cost_accuracy, cost_source, pricing_version, pricing_snapshot, conversation_id,
    request_kind, status, idempotency_key, metadata, occurred_at, created_at
  ) values (
    new.user_id,
    case when new.feature like 'agent_%' or new.feature like 'training_%' then 'training' else 'setter' end,
    new.feature,
    case
      when coalesce(new.request_kind, new.feature) like '%follow_up%' then 'follow_up_generated'
      when coalesce(new.request_kind, new.feature) in ('inbound_reply', 'activation_supervised', 'playground') then 'assistant_reply_generated'
      else 'ai_generation'
    end,
    coalesce(new.provider, 'anthropic'), 'llm', new.model,
    new.provider_response_id, new.input_tokens, new.output_tokens,
    new.cache_creation_input_tokens, new.cache_read_input_tokens, new.reasoning_tokens,
    new.provider_cost, new.provider_currency, new.exchange_rate_to_eur,
    coalesce(new.cost_eur, new.estimated_cost_eur),
    case
      when coalesce(new.cost_eur, new.estimated_cost_eur) is null then 'unknown'
      when new.cost_source = 'provider_reported' then 'provider_reported'
      when new.cost_eur is not null and new.input_tokens is not null then 'provider_usage_priced'
      else 'estimated'
    end,
    coalesce(new.cost_source, 'beta_ai_usage_mirror'), new.pricing_version, coalesce(new.pricing_snapshot, '{}'::jsonb),
    new.conversation_id, coalesce(new.request_kind, new.feature),
    coalesce(new.status, 'recorded'), 'beta_ai_usage:' || new.id::text,
    jsonb_build_object('legacy_table', 'beta_ai_usage', 'legacy_id', new.id),
    new.created_at, new.created_at
  ) on conflict do nothing;
  return new;
end;
$$;

revoke execute on function public.mirror_beta_ai_usage_to_ledger() from public, anon, authenticated;

drop trigger if exists beta_ai_usage_to_usage_ledger on public.beta_ai_usage;
create trigger beta_ai_usage_to_usage_ledger
after insert on public.beta_ai_usage
for each row execute function public.mirror_beta_ai_usage_to_ledger();

-- Append-only for browser roles. Trusted server jobs may add compensating events,
-- but never mutate historical usage rows.
comment on table public.usage_ledger is 'Append-only source of truth for attributable variable Angellos usage and cost.';
comment on column public.usage_ledger.cost_accuracy is 'provider_reported, provider_usage_priced, estimated, or unknown; unknown always has null cost_eur.';
