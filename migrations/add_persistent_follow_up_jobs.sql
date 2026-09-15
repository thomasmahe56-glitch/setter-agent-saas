-- Apply before deploying the new backend. Existing history is enqueued for a
-- bounded, indexed backfill; no browser or Mac process participates.
alter table public.beta_account_settings
  add column if not exists timezone text not null default 'Europe/Paris';
alter table public.beta_account_settings
  add column if not exists follow_up_config_version bigint not null default 1;

create table if not exists public.follow_up_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  conversation_id uuid not null references public.conversations(id) on delete cascade,
  stage text not null,
  stage_index integer not null,
  anchor_at timestamptz not null,
  due_at timestamptz not null,
  scheduled_at timestamptz not null,
  mode text not null check (mode in ('auto','manual')),
  status text not null default 'scheduled' check (status in
    ('scheduled','processing','sent','cancelled','blocked','failed','manual_required')),
  attempt_count integer not null default 0,
  last_error text,
  config_version bigint not null,
  idempotency_key text not null unique,
  provider_message_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  sent_at timestamptz,
  cancelled_at timestamptz
);
create index if not exists follow_up_jobs_due_idx
  on public.follow_up_jobs(scheduled_at, id) where status = 'scheduled';
create index if not exists follow_up_jobs_tenant_idx
  on public.follow_up_jobs(user_id, created_at desc);
create index if not exists follow_up_jobs_tenant_status_schedule_idx
  on public.follow_up_jobs(user_id, status, scheduled_at);
create index if not exists follow_up_jobs_conversation_idx
  on public.follow_up_jobs(conversation_id, status);

create table if not exists public.follow_up_refresh_queue (
  conversation_id uuid primary key references public.conversations(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  queued_at timestamptz not null default now(),
  claimed_at timestamptz
);
create index if not exists follow_up_refresh_queue_ready_idx
  on public.follow_up_refresh_queue(queued_at) where claimed_at is null;

alter table public.follow_up_jobs enable row level security;
alter table public.follow_up_refresh_queue enable row level security;
revoke all on public.follow_up_jobs from anon, authenticated;
revoke all on public.follow_up_refresh_queue from anon, authenticated;
grant select, insert, update on public.follow_up_jobs to service_role;
grant select, insert, update, delete on public.follow_up_refresh_queue to service_role;

create or replace function public.enqueue_follow_up_refresh() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if new.user_id is null then
    return new;
  end if;
  insert into public.follow_up_refresh_queue(conversation_id, user_id, queued_at, claimed_at)
  values (new.id, new.user_id, now(), null)
  on conflict (conversation_id) do update
    set queued_at = excluded.queued_at, claimed_at = null;
  return new;
end;
$$;
revoke execute on function public.enqueue_follow_up_refresh() from public, anon, authenticated;

drop trigger if exists conversations_follow_up_refresh on public.conversations;
create trigger conversations_follow_up_refresh after insert or update of
  history, agent_active, automation_mode, human_takeover, contact_status,
  status, last_inbound_at on public.conversations
  for each row execute function public.enqueue_follow_up_refresh();

create or replace function public.enqueue_follow_up_settings_refresh() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  if old.follow_up_config is distinct from new.follow_up_config or
     old.allowed_send_start is distinct from new.allowed_send_start or
     old.allowed_send_end is distinct from new.allowed_send_end or
     old.timezone is distinct from new.timezone then
    new.follow_up_config_version := old.follow_up_config_version + 1;
    insert into public.follow_up_refresh_queue(conversation_id, user_id, queued_at, claimed_at)
      select c.id, c.user_id, now(), null from public.conversations c
      where c.user_id = new.user_id and c.agent_active = true
      on conflict (conversation_id) do update
        set queued_at = excluded.queued_at, claimed_at = null;
  end if;
  return new;
end;
$$;
revoke execute on function public.enqueue_follow_up_settings_refresh() from public, anon, authenticated;
drop trigger if exists beta_settings_follow_up_refresh on public.beta_account_settings;
create trigger beta_settings_follow_up_refresh before update on public.beta_account_settings
  for each row execute function public.enqueue_follow_up_settings_refresh();

create or replace function public.enqueue_new_follow_up_settings() returns trigger
language plpgsql security invoker set search_path = '' as $$
begin
  insert into public.follow_up_refresh_queue(conversation_id, user_id)
    select c.id, c.user_id from public.conversations c
    where c.user_id = new.user_id and c.agent_active = true
    on conflict (conversation_id) do update
      set queued_at = now(), claimed_at = null;
  return new;
end;
$$;
revoke execute on function public.enqueue_new_follow_up_settings() from public, anon, authenticated;
drop trigger if exists beta_settings_follow_up_insert on public.beta_account_settings;
create trigger beta_settings_follow_up_insert after insert on public.beta_account_settings
  for each row execute function public.enqueue_new_follow_up_settings();

create or replace function public.claim_due_follow_up_jobs(batch_size integer default 50)
returns setof public.follow_up_jobs language plpgsql security invoker
set search_path = '' as $$
begin
  return query
  with candidates as (
    select id from public.follow_up_jobs
    where status = 'scheduled' and scheduled_at <= now()
    order by scheduled_at, id
    limit least(greatest(batch_size, 1), 100)
    for update skip locked
  )
  update public.follow_up_jobs j set status = 'processing',
    attempt_count = j.attempt_count + 1, updated_at = now()
  from candidates where j.id = candidates.id
  returning j.*;
end;
$$;
revoke execute on function public.claim_due_follow_up_jobs(integer) from public, anon, authenticated;
grant execute on function public.claim_due_follow_up_jobs(integer) to service_role;

-- A process that dies after claiming may have sent a DM. Never retry an
-- ambiguous external effect automatically: require a human reconciliation.
create or replace function public.reconcile_stale_follow_up_processing()
returns integer language plpgsql security invoker set search_path = '' as $$
declare affected integer;
begin
  update public.follow_up_jobs set status = 'manual_required',
    last_error = 'Worker stopped during an ambiguous provider attempt', updated_at = now()
  where status = 'processing' and updated_at < now() - interval '10 minutes';
  get diagnostics affected = row_count;
  return affected;
end;
$$;
revoke execute on function public.reconcile_stale_follow_up_processing() from public, anon, authenticated;
grant execute on function public.reconcile_stale_follow_up_processing() to service_role;

create or replace function public.release_stale_follow_up_refresh_claims()
returns integer language plpgsql security invoker set search_path = '' as $$
declare affected integer;
begin
  update public.follow_up_refresh_queue set claimed_at = null
  where claimed_at < now() - interval '10 minutes';
  get diagnostics affected = row_count;
  return affected;
end;
$$;
revoke execute on function public.release_stale_follow_up_refresh_claims() from public, anon, authenticated;
grant execute on function public.release_stale_follow_up_refresh_claims() to service_role;

insert into public.follow_up_refresh_queue(conversation_id, user_id)
select id, user_id from public.conversations where agent_active = true and user_id is not null
on conflict (conversation_id) do nothing;
