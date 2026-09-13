# Meta Instagram App Review readiness

Verified against Meta's official Instagram API Postman workspace and Graph API
version documentation on 2026-09-13. The integration uses **Instagram API with
Instagram Login**, `graph.instagram.com`, and Graph API `v26.0` by configuration.
Re-check Meta's dashboard labels and current API version immediately before the
review because both change independently of this repository.

## Minimal permissions

- `instagram_business_basic`
- `instagram_business_manage_messages`

Do not request comments or publishing permissions for the Setter V1.

## Configuration

- OAuth redirect: `https://<Railway backend>/oauth/meta/instagram/callback`
- Webhook callback: `https://<Railway backend>/webhooks/meta/instagram`
- Verify token: the secret configured as `META_WEBHOOK_VERIFY_TOKEN`
- Subscribe the Instagram app to the `messages` field in the current Meta App
  Dashboard. Confirm any account-level subscription step displayed by Meta.
- Never paste the app secret, access token, encryption key, or verify token into
  this document, Vercel public variables, screenshots, or browser storage.

## Screencast checklist

1. Sign in to Angellos.
2. Open Settings -> Integrations.
3. Select Connect Instagram.
4. Complete Meta's consent screen using an Instagram Professional test account.
5. Show the connected username and Meta Native status (never a token).
6. From a separate Instagram test user, initiate a DM.
7. Show the conversation appearing in Angellos.
8. Show the existing Setter producing a reply in supervised mode.
9. Approve/send the reply and show it arriving in Instagram.
10. Demonstrate auto mode only inside the allowed user-initiated messaging window.
11. Demonstrate Take over and Resume Angellos.
12. Disconnect and show that further automated sends are blocked.

## Review prerequisites

- Business app associated with the correct verified business portfolio.
- Privacy Policy and data deletion URL live and consistent with `META_DATA_FLOW.md`.
- App roles/test users have access to the Professional account.
- Advanced Access requested for both minimal permissions.
- Test credentials supplied through Meta's secure review fields, never committed.

## Known V1 limitations

- New webhook messages are synchronized from connection time onward.
- Historical Conversations API backfill is behind
  `META_INSTAGRAM_HISTORY_SYNC_ENABLED=false` and is not implemented in V1.
- Attachments are classified and stored as metadata; the AI receives a safe text
  fallback rather than downloading/analyzing media.
- OAuth uses a cryptographically random, tenant-bound, 10-minute, single-use
  `state`. PKCE is not enabled because the current official Instagram Login flow
  consulted for this implementation does not document a PKCE parameter; re-check
  this at App Review time and add it if Meta exposes/recommends it.
- A real Meta end-to-end run requires app credentials, a Professional account,
  webhook reachability, and review/test-user access. Automated tests mock Meta.
