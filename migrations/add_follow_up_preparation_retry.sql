-- Retry only failures that happened before any provider attempt. Keep the
-- original scheduled_at for audit/UI and store the next runnable instant
-- separately. Never automatically retry ambiguous external effects.
alter table public.follow_up_jobs
  add column if not exists next_retry_at timestamptz;

create index if not exists follow_up_jobs_claimable_idx
  on public.follow_up_jobs ((coalesce(next_retry_at, scheduled_at)), id)
  where status = 'scheduled';

create or replace function public.claim_due_follow_up_jobs(batch_size integer default 50)
returns setof public.follow_up_jobs language plpgsql security invoker
set search_path = '' as $$
begin
  return query
  with candidates as (
    select id from public.follow_up_jobs
    where status = 'scheduled' and coalesce(next_retry_at, scheduled_at) <= now()
    order by coalesce(next_retry_at, scheduled_at), id
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
