-- Compact LLM context state. Full conversations.history remains the audit source.
alter table public.conversations
  add column if not exists conversation_memory jsonb not null default '{}'::jsonb;

alter table public.conversations
  add column if not exists conversation_memory_updated_at timestamptz;

alter table public.conversations
  add column if not exists conversation_memory_through_message_count integer not null default 0
  check (conversation_memory_through_message_count >= 0);

comment on column public.conversations.conversation_memory is
  'Compact structured state used only to build LLM context; never replaces full history.';
