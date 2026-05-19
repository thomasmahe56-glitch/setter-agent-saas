## Feature : Automation Mode + Affiner avec Angelos
Déployé le 19 mai 2026.

### Automation Mode
Chaque conversation a un champ `automation_mode` (TEXT, default 'supervised') :
- `auto` : Angelos génère + envoie directement via ManyChat (fenêtre 24h active)
- `supervised` : Angelos génère, stocke dans `pending_message`, Thomas envoie manuellement
- `disabled` : message entrant stocké dans history, rien n'est généré

Colonnes ajoutées en base (migration : migrations/add_automation_mode.sql) :
- `automation_mode` TEXT DEFAULT 'supervised' CHECK (auto/supervised/disabled)
- `pending_message` TEXT
- `pending_message_at` TIMESTAMPTZ

Endpoints ajoutés dans main.py :
- PATCH /conversations/{id}/automation-mode → change le mode
- POST /conversations/{id}/ignore-pending → vide pending_message, marque ignored=True dans history
- POST /conversations/{id}/refine-pending → Angelos affine le message (voir ci-dessous)

Chaque message assistant dans le JSONB history a maintenant ces champs :
- `sent` (bool) : True si envoyé via ManyChat, False si supervisé en attente
- `ignored` (bool) : True si Thomas a ignoré le message
- `edited` (bool) : True si affiné par Angelos sur instruction de Thomas
- `generated_content` : version originale avant affinement
- `refinement_instruction` : instruction donnée par Thomas à Angelos

### Affiner avec Angelos
Dans la bannière pending_message du dashboard, bouton "✨ Demander à Angelos d'affiner".
Thomas tape une instruction en langage naturel ("Rends-le plus chaleureux", "Trop long, raccourcis"...).
Angelos régénère en tenant compte des 10 derniers messages du prospect.
Le pending_message est mis à jour immédiatement dans la bannière.
Le message affiné est tracé dans history avec edited=True + refinement_instruction.
→ Signal fort pour le feedback loop : compare generated_content vs content final.

### Contrainte Instagram 24h
Le mode `auto` (envoi ManyChat direct) ne fonctionne que dans la fenêtre 24h active.
Les relances (J+3, J+10) sont toujours en mode supervisé : Copier + lien ig.me/m/{display_name}.

### UI Dashboard
- Sélecteur Auto / Supervisé / Off dans le header du ConversationPanel
- Badge coloré dans ProspectList : vert (auto), orange (supervisé), gris (off)
- Bannière pending : Copier, Ouvrir Instagram, Affiner avec Angelos, Ignorer
