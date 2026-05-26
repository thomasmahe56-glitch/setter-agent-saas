# Setter Agent SaaS — Angellos

Agent setter Instagram et WhatsApp multi-client. Qualifie les 
prospects en DM et les oriente vers un appel ou une page de vente.

---

## Architecture

**Une seule infrastructure, plusieurs clients.**

```
Railway (1 instance backend)
    └── Supabase (1 base de données)
            ├── Client A (user_id: uuid-a)
            ├── Client B (user_id: uuid-b)
            └── Client C (user_id: uuid-c)
```

Chaque client a son propre compte Supabase Auth. Toutes les tables 
sont isolées par `user_id` via Row Level Security (RLS). Un client 
ne voit jamais les données d'un autre.

Tu n'as **pas** besoin de créer une instance Railway ou une base 
Supabase par client. Tout tourne sur une seule infrastructure.

---

## Ajouter un nouveau client

### 1. Créer son compte dans Supabase Auth

1. Ouvre ton projet Supabase → Authentication → Users
2. Clique sur **Invite user** ou **Add user**
3. Renseigne son email et un mot de passe temporaire
4. Copie son `user_id` (UUID affiché dans la liste)

### 2. Transmettre ses identifiants dashboard

Envoie-lui :
- L'URL de ton dashboard SaaS
- Son email
- Son mot de passe temporaire (il pourra le changer)

### 3. Il configure son Training Center

Une fois connecté, le client renseigne dans le dashboard :

- **Profil business** : nom, offre, prix, résultats, ton de voix
- **Avatar client** : situation, problèmes, peurs, objections, 
  mots exacts
- **Règles DM** : questions de qualification, signaux d'achat, 
  conditions d'arrêt

Ces données sont injectées automatiquement dans le prompt 
d'Angellos pour ce client.

### 4. Connecter ManyChat

Dans ManyChat, créer deux flows :

**Flow principal (réponse aux messages) :**
POST {RAILWAY_URL}/webhook
Header : X-Webhook-Secret: {WEBHOOK_SECRET}
Body :
{
  "username": "{{first name}}",
  "message": "{{last input text}}",
  "subscriber_id": "{{subscriber id}}"
}
Lire le champ `agent_response` dans la réponse pour envoyer 
le message au prospect.

**Flow relance H23 (automation ManyChat) :**
POST {RAILWAY_URL}/follow-ups/manychat-auto-23h
Header : Authorization: Bearer {TOKEN_SUPABASE_CLIENT}
Body : { "subscriber_id": "{{subscriber id}}" }
Mapper le champ `message` vers une variable ManyChat et ajouter 
une condition `message is not empty` avant l'envoi.

### 5. Connecter WhatsApp (optionnel)

Dans Meta for Developers :
1. Créer ou configurer une app WhatsApp Business Platform
2. Webhook callback : GET /webhooks/whatsapp et 
   POST /webhooks/whatsapp
3. Verify token : valeur de WHATSAPP_VERIFY_TOKEN
4. S'abonner aux événements messages

---

## Installation initiale (une seule fois)

### 1. Créer le projet Supabase

1. Créer un compte sur supabase.com
2. Créer un nouveau projet
3. Dans SQL Editor, exécuter les scripts dans cet ordre :
   - Script de création des tables de base (voir section Tables)
   - migrations/add_whatsapp_channel.sql
   - migrations/add_automation_mode.sql
   - migrations/add_training_center.sql
4. Récupérer dans Settings > API :
   - Project URL → SUPABASE_URL (ajouter /rest/v1 à la fin)
   - service_role key → SUPABASE_KEY

### 2. Créer le projet Railway

1. Aller sur railway.app
2. New Project → Deploy from GitHub (connecter ce repo)
3. Dans Variables, ajouter toutes les variables du .env.example

Générer les secrets :
openssl rand -hex 32   # pour WEBHOOK_SECRET
openssl rand -hex 32   # pour DASHBOARD_SECRET

---

## Variables d'environnement

| Variable | Description | Obligatoire |
|----------|-------------|-------------|
| SUPABASE_URL | URL Supabase + /rest/v1 | ✅ |
| SUPABASE_KEY | Clé service Supabase | ✅ |
| ANTHROPIC_API_KEY | Clé API Anthropic | ✅ |
| WEBHOOK_SECRET | Secret pour /webhook | ✅ |
| OWNER_USER_ID | UUID Supabase Auth du fondateur | Recommandé |
| DASHBOARD_SECRET | Secret legacy | Optionnel |
| MANYCHAT_TOKEN | Token API ManyChat | ✅ |
| WHATSAPP_ACCESS_TOKEN | Token Cloud API WhatsApp | Si WhatsApp |
| WHATSAPP_PHONE_NUMBER_ID | Phone Number ID Meta | Si WhatsApp |
| WHATSAPP_VERIFY_TOKEN | Token vérification webhook | Si WhatsApp |
| META_APP_SECRET | Secret app Meta | Recommandé |
| GRAPH_API_VERSION | Version Graph API (défaut v23.0) | Optionnel |
| BUSINESS_NAME | Nom du business (fallback) | ✅ |
| COACH_NAME | Prénom + nom du coach (fallback) | ✅ |
| AGENT_NAME | Nom de l'agent IA | ✅ |
| URL_PAGE | URL page de vente (fallback) | ✅ |
| URL_CALL | URL Calendly (fallback) | ✅ |
| CONTACT_EMAIL | Email pour partenariats | ✅ |
| NICHE_CONTEXT | Contexte métier injecté (fallback) | Recommandé |

---

## Endpoints principaux

| Méthode | Endpoint | Description |
|---------|----------|-------------|
| POST | /webhook | Message ManyChat → réponse agent |
| GET | /webhooks/whatsapp | Vérification webhook Meta |
| POST | /webhooks/whatsapp | Messages WhatsApp Cloud API |
| GET | /conversations | Liste conversations du client |
| GET | /conversations/summary | Liste légère sans historique |
| GET | /conversations/{id} | Détail complet |
| POST | /activate | Active l'agent pour un contact |
| POST | /deactivate | Désactive l'agent |
| GET | /follow-ups/due | Relances en attente |
| POST | /follow-ups/preview | Prévisualise une relance IA |
| POST | /follow-ups/manychat-auto-23h | Relance H23 depuis ManyChat |
| POST | /feedback-loop | Analyse et améliore le prompt |
| POST | /playground | Test du prompt en sandbox |
| GET | /agent/training-center | Profil complet du client |
| POST | /agent/profile/save | Sauvegarde profil business |
| POST | /agent/avatar/generate | Génère avatar client via IA |
| POST | /agent/avatar/save | Sauvegarde avatar client |
| POST | /agent/sales-rules/generate | Génère règles DM via IA |
| POST | /agent/sales-rules/save | Sauvegarde règles DM |
| POST | /agent/prompt/rebuild | Reconstruit le prompt actif |

Tous les endpoints dashboard requièrent :
Authorization: Bearer <access_token Supabase>

---

## Modes de fonctionnement par conversation

| Mode | Comportement |
|------|-------------|
| auto | Génère et envoie directement (fenêtre 24h uniquement) |
| supervised | Génère et stocke dans pending_message |
| disabled | Stocke les messages, ne génère rien |

---

## Relances automatiques

| Stage | Déclenchement | Mode |
|-------|--------------|------|
| auto_23h | Entre 23h et 24h après dernier message | Auto ManyChat |
| j3 | Entre 72h et 240h | Supervisé |
| j10 | Après 240h | Supervisé |

---

## Stack technique

- Backend : FastAPI + Python 3.11, déployé sur Railway
- Base de données : Supabase (PostgreSQL + Auth + RLS)
- IA : Anthropic Claude Sonnet (réponses) + Claude Opus (analyse)
- Instagram : ManyChat webhook
- WhatsApp : Meta Cloud API
- Dashboard : Next.js déployé sur Vercel (setter-dashboard-saas)
