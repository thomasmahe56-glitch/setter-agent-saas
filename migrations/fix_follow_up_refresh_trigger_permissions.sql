-- The refresh queue is backend-only. Conversation/settings writes can run as
-- authenticated users; the trigger must enqueue with its postgres owner's
-- privileges, while direct queue access remains revoked from client roles.
-- All three functions already use an empty search_path and qualified tables.
alter function public.enqueue_follow_up_refresh() security definer;
alter function public.enqueue_follow_up_settings_refresh() security definer;
alter function public.enqueue_new_follow_up_settings() security definer;

revoke execute on function public.enqueue_follow_up_refresh() from public, anon, authenticated;
revoke execute on function public.enqueue_follow_up_settings_refresh() from public, anon, authenticated;
revoke execute on function public.enqueue_new_follow_up_settings() from public, anon, authenticated;
