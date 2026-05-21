# Setter Agent SaaS

Agent setter Instagram et WhatsApp multi-client. Qualifie les prospects en DM et les oriente vers un appel ou une page de vente.

Une instance Railway = un client. Tout le contexte métier est configuré via variables d'environnement.

---

## Déploiement d'un nouveau client

### 1. Créer un projet Supabase

1. Créer un compte sur [supabase.com](https://supabase.com)
2. Créer un nouveau projet
3. Dans **SQL Editor**, exécuter le script de création des tables (voir section Tables)
4. Récupérer dans **Settings > API** :
   - **Project URL** → `SUPABASE_URL` (ajouter `/rest/v1` à la fin)
   - **service_role key** → `SUPABASE_KEY`

### 2. Tables Supabase requises

```sql
-- Conversations
create table conversations (
  id uuid default gen_random_uuid() primary key,
  user_id uuid,
  created_at timestamptz default now(),
  username text not null,
  channel text default 'instagram' check (channel in ('instagram', 'whatsapp')),
  external_contact_id text not null,
  phone_e164 text,
  display_name text,
  message text,
  response text,
  status text default 'nouveau',
  agent_active boolean default true,
  pending_opener boolean default false,
  last_inbound_at timestamptz,
  transport_metadata jsonb default '{}',
  history jsonb default '[]'
);

create unique index conversations_user_channel_external_idx
on conversations (user_id, channel, external_contact_id);

-- Insights (feedback loop)
create table insights (
  id uuid default gen_random_uuid() primary key,
  created_at timestamptz default now(),
  conversations_analyzed int,
  date_range_start timestamptz,
  date_range_end timestamptz,
  pain_points jsonb,
  objections jsonb,
  converting_profiles jsonb,
  drop_off_stages jsonb,
  business_suggestions jsonb,
  prompt_current text,
  prompt_proposed text,
  prompt_diff jsonb,
  status text default 'pending'
);

-- Versions de prompt
create table prompt_versions (
  id uuid default gen_random_uuid() primary key,
  created_at timestamptz default now(),
  content text not null,
  is_active boolean default false,
  source text,
  insight_id uuid references insights(id)
);
```

### 3. Créer un projet Railway

1. Aller sur [railway.app](https://railway.app)
2. **New Project → Deploy from GitHub** (connecter ce repo)
3. Dans **Variables**, ajouter toutes les variables du `.env.example`

**Générer les secrets :**
```bash
openssl rand -hex 32   # pour WEBHOOK_SECRET
openssl rand -hex 32   # pour DASHBOARD_SECRET
```

### 4. Configurer les variables d'environnement

Copier `.env.example`, remplir toutes les valeurs et les coller dans Railway > Variables.

Le point clé : `NICHE_CONTEXT` définit l'avatar client et le positionnement du coach. Railway permet de coller du texte multi-ligne directement dans l'éditeur de variables.

**Exemple pour un coach running :**
```
POSITIONNEMENT :
- Approche scientifique, long terme
- Expert coach, pas vendeur

AVATAR CLIENT : Coureur régulier, souvent blessé ou limité, frustré de stagner.

SOURCES D'ENTRÉE :
- "Analyse" → script qualification
- Nouveau follower → script d'accueil
```

### 5. Connecter ManyChat

Dans ManyChat :
1. Créer un flow qui appelle `POST /webhook` avec :
   - Header `X-Webhook-Secret: {WEBHOOK_SECRET}`
   - Body : `{"username": "{{first name}}", "message": "{{last input text}}", "subscriber_id": "{{subscriber id}}"}`
2. Lire le champ `agent_response` dans la réponse pour envoyer le message

Pour la relance automatique 23h :
- Flow déclenché par automation ManyChat → `POST /follow-ups/manychat-auto-23h`
- Body : `{"subscriber_id": "{{subscriber id}}"}`

### 6. Connecter WhatsApp Cloud API

Dans Meta for Developers :
1. Créer/configurer une app WhatsApp Business Platform.
2. Renseigner le webhook callback : `GET/POST /webhooks/whatsapp`
3. Verify token : valeur de `WHATSAPP_VERIFY_TOKEN`
4. S'abonner aux événements `messages`

Variables Railway à ajouter :
- `WHATSAPP_ACCESS_TOKEN`
- `WHATSAPP_PHONE_NUMBER_ID`
- `WHATSAPP_VERIFY_TOKEN`
- `META_APP_SECRET`
- `GRAPH_API_VERSION` (optionnel, défaut `v23.0`)

Le mode `auto` envoie directement les réponses WhatsApp tant que la fenêtre de service 24h est ouverte. Les relances J+3/J+10 restent supervisées sauf ajout futur de templates WhatsApp approuvés.

---

## Variables d'environnement

| Variable | Description | Obligatoire |
|----------|-------------|-------------|
| `SUPABASE_URL` | URL Supabase + `/rest/v1` | ✅ |
| `SUPABASE_KEY` | Clé service Supabase | ✅ |
| `ANTHROPIC_API_KEY` | Clé API Anthropic | ✅ |
| `WEBHOOK_SECRET` | Secret pour l'endpoint `/webhook` | ✅ |
| `OWNER_USER_ID` | UUID Supabase Auth du coach propriétaire de cette instance | ✅ |
| `DASHBOARD_SECRET` | Ancien secret dashboard, conservé seulement si un outil local l'utilise encore | Optionnel |
| `MANYCHAT_TOKEN` | Token API ManyChat | ✅ |
| `WHATSAPP_ACCESS_TOKEN` | Token Cloud API WhatsApp | Requis pour WhatsApp |
| `WHATSAPP_PHONE_NUMBER_ID` | Phone Number ID Meta WhatsApp | Requis pour WhatsApp |
| `WHATSAPP_VERIFY_TOKEN` | Token de vérification du webhook WhatsApp | Requis pour WhatsApp |
| `META_APP_SECRET` | Secret app Meta pour valider `X-Hub-Signature-256` | Recommandé |
| `GRAPH_API_VERSION` | Version Graph API, défaut `v23.0` | Optionnel |
| `BUSINESS_NAME` | Nom du business | ✅ |
| `COACH_NAME` | Prénom + nom du coach | ✅ |
| `AGENT_NAME` | Nom de l'agent IA | ✅ |
| `URL_PAGE` | URL de la page de vente | ✅ |
| `URL_CALL` | URL Calendly (appel découverte) | ✅ |
| `CONTACT_EMAIL` | Email pour partenariats | ✅ |
| `NICHE_CONTEXT` | Avatar client + positionnement | Recommandé |

---

## Endpoints principaux

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| `POST` | `/webhook` | Reçoit un message ManyChat, génère et envoie la réponse |
| `GET` | `/webhooks/whatsapp` | Vérification webhook Meta WhatsApp |
| `POST` | `/webhooks/whatsapp` | Reçoit les messages WhatsApp Cloud API |
| `GET` | `/conversations` | Liste toutes les conversations |
| `POST` | `/activate` | Active l'agent pour un contact |
| `POST` | `/deactivate` | Désactive l'agent pour un contact |
| `GET` | `/follow-ups/due` | Relances en attente |
| `POST` | `/follow-ups/preview` | Prévisualise une relance IA |
| `POST` | `/feedback-loop` | Analyse les conversations et propose une amélioration du prompt |
| `POST` | `/playground` | Teste le prompt en mode sandbox |

Les endpoints dashboard requièrent `Authorization: Bearer <access_token Supabase>`.
Le token est validé via Supabase Auth, donc `SUPABASE_JWT_SECRET` n'est plus nécessaire.
Si `OWNER_USER_ID` est configuré, seul cet utilisateur Supabase Auth peut accéder au dashboard de cette instance.
