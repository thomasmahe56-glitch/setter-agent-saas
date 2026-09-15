-- Isolated angellos-follow-up-test database only. The original minimal
-- baseline omitted the provider selector; synthetic auto jobs must target
-- the disabled Meta adapter rather than the ManyChat default.
alter table public.conversations
  add column if not exists messaging_provider text not null default 'meta_instagram';
