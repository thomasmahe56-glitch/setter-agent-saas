-- Apply after the persistent follow-up jobs migration and before deploying the
-- worker that calls this RPC. The conversation update is atomic and idempotent:
-- a prospect reply arriving after Meta accepts the DM cannot erase its history.
create or replace function public.append_sent_follow_up_history(
  p_job_id uuid,
  p_user_id uuid,
  p_conversation_id uuid,
  p_message jsonb,
  p_expected_history jsonb,
  p_expected_inbound_at timestamptz,
  p_response text
) returns boolean
language plpgsql security invoker set search_path = '' as $$
declare appended boolean;
begin
  if p_message->>'follow_up_job_id' is distinct from p_job_id::text or
     p_message->>'role' is distinct from 'assistant' or
     p_message->>'sent' is distinct from 'true' then
    return false;
  end if;

  update public.conversations c
  set history = coalesce(c.history, '[]'::jsonb) || jsonb_build_array(p_message),
      response = case
        when coalesce(c.history, '[]'::jsonb) = coalesce(p_expected_history, '[]'::jsonb)
         and c.last_inbound_at is not distinct from p_expected_inbound_at
        then p_response else c.response end,
      status = case
        when coalesce(c.history, '[]'::jsonb) = coalesce(p_expected_history, '[]'::jsonb)
         and c.last_inbound_at is not distinct from p_expected_inbound_at
         and c.status not in ('appel_booke', 'signe')
        then 'en_cours' else c.status end
  where c.id = p_conversation_id and c.user_id = p_user_id
    and exists (
      select 1 from public.follow_up_jobs j
      where j.id = p_job_id and j.user_id = p_user_id
        and j.conversation_id = p_conversation_id and j.status = 'sent'
    )
    and not exists (
      select 1 from jsonb_array_elements(coalesce(c.history, '[]'::jsonb)) m
      where m->>'follow_up_job_id' = p_job_id::text
    )
  returning true into appended;

  if appended then return true; end if;
  return exists (
    select 1 from public.conversations c,
      lateral jsonb_array_elements(coalesce(c.history, '[]'::jsonb)) m
    where c.id = p_conversation_id and c.user_id = p_user_id
      and m->>'follow_up_job_id' = p_job_id::text
  );
end;
$$;
revoke execute on function public.append_sent_follow_up_history(uuid,uuid,uuid,jsonb,jsonb,timestamptz,text)
  from public, anon, authenticated;
grant execute on function public.append_sent_follow_up_history(uuid,uuid,uuid,jsonb,jsonb,timestamptz,text)
  to service_role;
