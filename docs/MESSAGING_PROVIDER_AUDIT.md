# Messaging provider audit (2026-09-13)

## Before

```text
Instagram -> ManyChat /webhook -> handle_inbound_message -> AI -> ManyChat Send API
```

- ManyChat authenticates `/webhook` with a tenant webhook secret.
- `subscriber_id` + tenant + `channel=instagram` locate/create a conversation.
- `handle_inbound_message` owns CRM history, memory, AI generation, credits, and
  auto/supervised/disabled behavior.
- ManyChat outbound was selected implicitly whenever `channel=instagram`.
- Duplicate protection existed in an in-memory replay cache, history message IDs,
  and the optional `processed_inbound_events` persistent ledger.
- Retry behavior was primarily the pending-delivery path; provider routing was not
  explicit on the conversation.

## After

```text
                         +-- ManyChatProvider --------+
Inbound provider -------+                             +-> Angellos core
                         +-- MetaInstagramProvider ---+
                                  |
                                  +-> same CRM / AI / modes / credits
                                  +-> provider-specific outbound
```

- `messaging_provider` and `messaging_connection_id` select exactly one outbound
  transport per conversation; existing rows default to ManyChat.
- `beta_account_settings.active_messaging_provider` defaults to ManyChat, switches
  to Meta Native only after successful OAuth, and switches back on disconnect. The
  inactive provider acknowledges webhooks without entering the Setter pipeline.
- Meta events normalize before entering the existing Setter pipeline.
- Meta tenant matching uses only the authorized Instagram account ID.
- Meta tokens live only in the service-role-only provider-neutral connection table.
- ManyChat routes, credentials, behavior, and existing tests remain intact.
