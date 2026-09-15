-- Isolated angellos-follow-up-test database only. The minimal test baseline
-- omitted the connection selector present in production. These UUIDs are
-- synthetic routing placeholders; the Railway test service has Meta outbound
-- disabled and must never use them for real messages.
alter table public.conversations
  add column if not exists messaging_connection_id uuid;

update public.conversations
set messaging_connection_id = '00000000-0000-4000-8000-000000000010'
where user_id = '00000000-0000-4000-8000-000000000001'
  and id in (
    '00000000-0000-4000-8000-000000000003',
    '00000000-0000-4000-8000-000000000004'
  )
  and messaging_provider = 'meta_instagram';
