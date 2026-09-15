-- Apply after add_persistent_follow_up_jobs and the trigger-permission fix.
-- A provider/connection/recipient switch, explicit opt-out timestamp, or
-- active tenant provider switch invalidates future transport assumptions.
alter table public.conversations
  add column if not exists opted_out_at timestamptz;
alter table public.beta_account_settings
  add column if not exists active_messaging_provider text not null default 'meta_instagram';

drop trigger if exists conversations_follow_up_refresh on public.conversations;
create trigger conversations_follow_up_refresh after insert or update of
  history, agent_active, automation_mode, human_takeover, contact_status,
  opted_out_at, status, last_inbound_at, messaging_provider,
  messaging_connection_id, external_contact_id on public.conversations
  for each row execute function public.enqueue_follow_up_refresh();

create or replace function public.enqueue_follow_up_settings_refresh()
returns trigger language plpgsql security definer set search_path = '' as $$
begin
  if old.follow_up_config is distinct from new.follow_up_config or
     old.allowed_send_start is distinct from new.allowed_send_start or
     old.allowed_send_end is distinct from new.allowed_send_end or
     old.timezone is distinct from new.timezone or
     old.active_messaging_provider is distinct from new.active_messaging_provider then
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
revoke execute on function public.enqueue_follow_up_settings_refresh()
  from public, anon, authenticated;
