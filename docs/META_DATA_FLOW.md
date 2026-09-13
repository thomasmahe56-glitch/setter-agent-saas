# Meta Native data flow

```text
Instagram user
  -> Meta webhook
  -> Angellos backend
  -> Supabase conversations / event ledger
  -> configured AI provider (conversation text needed for reply)
  -> Angellos backend eligibility gate
  -> Meta Send API
  -> Instagram user
```

## Data handling

| Stage | Received | Stored | Sent onward | Retention |
| --- | --- | --- | --- | --- |
| OAuth | authorization code, account identity, granted permissions | account ID, username, scopes, expiry, AES-256-GCM encrypted token | code/token requests to Meta only | OAuth state expires after 10 minutes and is single-use; connection record until disconnect/deletion |
| Webhook | sender scoped ID, message ID/text/timestamp, attachment metadata | conversation history, provider/connection IDs, idempotency status | relevant conversation text to the configured AI provider | follows the product's conversation retention policy; deletion hook is the existing conversation delete path |
| AI generation | bounded conversation context | generated reply and usage ledger | selected context to the configured AI subprocesser | follows existing Angellos conversation and usage retention |
| Send API | eligible recipient scoped ID and reply text | delivery status/IDs, never the access token | recipient ID/text to Meta | follows existing conversation retention |

## Technical controls

- Tenant identity comes only from `entry.id -> messaging_connections -> user_id`.
- Tokens are encrypted at rest, decrypted only in the backend, and never returned
  by the integration status endpoint.
- Webhooks require the configured verify token for setup and
  `X-Hub-Signature-256` HMAC validation for events.
- Provider + connection + event identity is reserved before AI work.
- Explicit opt-out deterministically disables automation before AI generation.
- Disconnect wipes the usable encrypted token and disables linked conversations.
- Human takeover keeps inbound synchronization but blocks automatic generation/send.
- Existing conversation deletion supports prospect/conversation erasure; a product-
  wide export/retention schedule remains a separate compliance policy decision.
