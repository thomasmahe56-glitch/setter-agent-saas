# Setter Agent SaaS — Angellos

Multi-client Instagram and WhatsApp setter agent. It qualifies 
prospects in DMs and guides them toward a call or a sales page.

---

## Architecture

**One infrastructure, multiple clients.**

```
Railway (1 backend instance)
    └── Supabase (1 database)
            ├── Client A (user_id: uuid-a)
            ├── Client B (user_id: uuid-b)
            └── Client C (user_id: uuid-c)
```

Each client has their own Supabase Auth account. All tables are 
isolated by `user_id` through Row Level Security (RLS). A client 
never sees another client's data.

You do **not** need to create one Railway instance or one Supabase 
database per client. Everything runs on a single infrastructure.

---

## Add A New Client

### 1. Create Their Supabase Auth Account

1. Open your Supabase project → Authentication → Users
2. Click **Invite user** or **Add user**
3. Enter their email and a temporary password
4. Copy their `user_id` (UUID shown in the list)

### 2. Send Their Dashboard Credentials

Send them:
- Your SaaS dashboard URL
- Their email
- Their temporary password (they can change it later)

### 3. They Configure Their Training Center

Once logged in, the client fills in the dashboard:

- **Business profile**: name, offer, price, results, voice tone
- **Client avatar**: situation, problems, fears, objections, 
  exact words
- **DM rules**: qualification questions, buying signals, 
  stop conditions

This data is automatically injected into Angellos' prompt for this 
client.

### 4. Connect ManyChat

In ManyChat, create two flows:

**Main flow (message replies):**
POST {RAILWAY_URL}/webhook
Header: X-Webhook-Secret: {WEBHOOK_SECRET}
Body:
{
  "username": "{{first name}}",
  "message": "{{last input text}}",
  "subscriber_id": "{{subscriber id}}"
}
Map `agent_response`, `should_send`, and `mode` from the response.
Add a condition before any Instagram send step:
- If `should_send` is `true`, send `agent_response`.
- If `should_send` is `false`, do not send anything.

The dashboard mode is the source of truth. In `supervised` mode,
the backend saves the suggested reply as `pending_message` but returns
an empty `agent_response` so ManyChat cannot accidentally send it.
In `off` mode, the backend generates nothing and returns an empty
`agent_response`.

**H23 follow-up flow (ManyChat automation):**
POST {RAILWAY_URL}/follow-ups/manychat-auto-23h
Header: Authorization: Bearer {TOKEN_SUPABASE_CLIENT}
Body: { "subscriber_id": "{{subscriber id}}" }
Map the `message` field to a ManyChat variable and add a 
`message is not empty` condition before sending.

### 5. Connect WhatsApp (Optional)

In Meta for Developers:
1. Create or configure a WhatsApp Business Platform app
2. Webhook callback: GET /webhooks/whatsapp and 
   POST /webhooks/whatsapp
3. Verify token: value of WHATSAPP_VERIFY_TOKEN
4. Subscribe to message events

---

## Initial Setup (One Time)

### 1. Create The Supabase Project

1. Create an account on supabase.com
2. Create a new project
3. In SQL Editor, run the scripts in this order:
   - Base table creation script (see Tables section)
   - migrations/add_whatsapp_channel.sql
   - migrations/add_automation_mode.sql
   - migrations/add_training_center.sql
4. Retrieve from Settings > API:
   - Project URL → SUPABASE_URL (append /rest/v1 at the end)
   - service_role key → SUPABASE_KEY

### 2. Create The Railway Project

1. Go to railway.app
2. New Project → Deploy from GitHub (connect this repo)
3. In Variables, add all variables from .env.example

Generate secrets:
openssl rand -hex 32   # for WEBHOOK_SECRET
openssl rand -hex 32   # for DASHBOARD_SECRET

---

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| SUPABASE_URL | Supabase URL + /rest/v1 | ✅ |
| SUPABASE_KEY | Supabase service key | ✅ |
| ANTHROPIC_API_KEY | Anthropic API key | ✅ |
| WEBHOOK_SECRET | Secret for /webhook | ✅ |
| OWNER_USER_ID | Founder Supabase Auth UUID | Recommended |
| DASHBOARD_SECRET | Legacy secret | Optional |
| MANYCHAT_TOKEN | ManyChat API token | ✅ |
| WHATSAPP_ACCESS_TOKEN | WhatsApp Cloud API token | If WhatsApp |
| WHATSAPP_PHONE_NUMBER_ID | Meta Phone Number ID | If WhatsApp |
| WHATSAPP_VERIFY_TOKEN | Webhook verification token | If WhatsApp |
| META_APP_SECRET | Meta app secret | Recommended |
| GRAPH_API_VERSION | Graph API version (default v23.0) | Optional |
| BUSINESS_NAME | Business name (fallback) | ✅ |
| COACH_NAME | Coach first + last name (fallback) | ✅ |
| AGENT_NAME | AI agent name | ✅ |
| URL_PAGE | Sales page URL (fallback) | ✅ |
| URL_CALL | Calendly URL (fallback) | ✅ |
| CONTACT_EMAIL | Email for partnerships | ✅ |
| NICHE_CONTEXT | Injected business context (fallback) | Recommended |

---

## Main Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | /webhook | ManyChat message → agent response |
| GET | /webhooks/whatsapp | Meta webhook verification |
| POST | /webhooks/whatsapp | WhatsApp Cloud API messages |
| GET | /conversations | Client conversation list |
| GET | /conversations/summary | Lightweight list without history |
| GET | /conversations/{id} | Full detail |
| POST | /activate | Activate the agent for a contact |
| POST | /deactivate | Deactivate the agent |
| GET | /follow-ups/due | Pending follow-ups |
| POST | /follow-ups/preview | Preview an AI follow-up |
| POST | /follow-ups/manychat-auto-23h | H23 follow-up from ManyChat |
| POST | /feedback-loop | Analyze and improve the prompt |
| POST | /reviews/daily | Cron-compatible autonomous daily conversation review, protected by DASHBOARD_SECRET |
| GET | /reviews/daily | Dashboard/API view of stored daily reviews |
| PATCH | /reviews/{id}/lesson-status | Approve/reject a review lesson for controlled prompt injection |
| POST | /playground | Test the prompt in sandbox |
| GET | /agent/training-center | Full client profile |
| POST | /agent/profile/save | Save business profile |
| POST | /agent/avatar/generate | Generate client avatar with AI |
| POST | /agent/avatar/save | Save client avatar |
| POST | /agent/sales-rules/generate | Generate DM rules with AI |
| POST | /agent/sales-rules/save | Save DM rules |
| POST | /agent/prompt/rebuild | Rebuild the active prompt |

Dashboard auth: send the Supabase access token in the Authorization header.

`POST /reviews/daily` is for Railway cron or an ops trigger and requires
`X-Dashboard-Secret: {DASHBOARD_SECRET}` plus a JSON body containing
`user_id`, optional `review_date` (`YYYY-MM-DD`), optional `limit`, and
optional `conversation_id`. It stores every generated lesson as `candidate`.
Only lessons later marked `approved` by `PATCH /reviews/{id}/lesson-status`
are appended to Angellos' live prompt context, capped server-side to prevent
prompt bloat.

---

## Conversation Operating Modes

| Mode | Behavior |
|------|----------|
| auto | Generates and sends directly (24h window only) |
| supervised | Generates and stores in pending_message |
| disabled | Stores messages, generates nothing |

---

## Automatic Follow-Ups

| Stage | Trigger | Mode |
|-------|---------|------|
| auto_23h | Between 23h and 24h after the last message | Auto ManyChat |
| j3 | Between 72h and 240h | Supervised |
| j10 | After 240h | Supervised |

---

## Technical Stack

- Backend: FastAPI + Python 3.11, deployed on Railway
- Database: Supabase (PostgreSQL + Auth + RLS)
- AI: Anthropic Claude Sonnet (responses) + Claude Opus (analysis)
- Instagram: ManyChat webhook
- WhatsApp: Meta Cloud API
- Dashboard: Next.js deployed on Vercel (setter-dashboard-saas)
