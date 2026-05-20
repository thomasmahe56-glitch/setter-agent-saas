# Journal des interventions Codex

Ce fichier sert a documenter les interventions effectuees par Codex dans ce projet.

Pour chaque intervention, ajouter une entree datee avec :

- le contexte de la demande
- les fichiers consultes ou modifies
- les changements effectues
- les verifications lancees
- les limites ou suites a prevoir

## 2026-05-01

### Configuration Railway du secret webhook

Contexte :

- Demande utilisateur : configurer la suite necessaire apres la protection du webhook.

Fichiers consultes :

- `AGENT_LOG.md`
- `.env` dans le dossier projet principal, sans afficher les secrets

Fichiers modifies :

- `.gitignore`
- `AGENT_LOG.md`

Actions effectuees :

- Verification du lien Railway local : projet `setter-agent`, environnement `production`, service `setter-agent`.
- Ajout de la variable `WEBHOOK_SECRET` dans Railway depuis la valeur locale du `.env`.
- Verification de presence de `WEBHOOK_SECRET` via `railway run`, sans afficher sa valeur.
- Ajout de `.DS_Store` au `.gitignore`.
- Premiere tentative de `railway up --detach` interrompue car elle ne progressait pas apres l'indexation.
- Deuxieme tentative de `railway up --detach --verbose` interrompue car elle restait bloquee sur `Indexing...`.
- Troisieme tentative de `railway up --ci` interrompue apres un blocage prolonge sur `Indexing...`.
- Creation d'un commit local `Secure webhook with secret header`.
- Tentative de `git push origin main` refusee par GitHub avec une erreur 403 de droits d'ecriture.

Verifications :

- `railway status` confirme le projet, l'environnement et le service.
- `railway run sh -c 'test -n "$WEBHOOK_SECRET" ...'` confirme que la variable est presente en production.
- Le deploiement automatique n'a pas pu etre finalise depuis Codex a cause du blocage `railway up` et du refus d'ecriture GitHub.

Limites et suites :

- Configurer l'outil qui appelle le webhook pour envoyer le header `X-Webhook-Secret` avec cette meme valeur.
- Si l'outil appelant ne permet pas d'ajouter un header custom, prevoir une adaptation du webhook.
- Donner a ce poste un acces d'ecriture GitHub valide ou lancer manuellement le deploiement Railway depuis l'interface.

### Protection du webhook

Contexte :

- Demande utilisateur : appliquer la prochaine etape recommandee, c'est-a-dire securiser le webhook.

Fichiers consultes :

- `main.py`
- `test.py`
- `AGENT_LOG.md`

Fichiers modifies :

- `main.py`
- `test.py`
- `AGENT_LOG.md`
- `.env` dans le dossier projet principal, sans journaliser la valeur du secret

Changements effectues :

- Ajout de la variable d'environnement `WEBHOOK_SECRET`.
- Ajout d'une verification obligatoire du header `X-Webhook-Secret` sur `POST /webhook`.
- Ajout d'une erreur HTTP 401 si le secret fourni est absent ou incorrect.
- Ajout d'une erreur HTTP 500 claire si `WEBHOOK_SECRET` n'est pas configure.
- Ajout d'une erreur HTTP 500 claire si `ANTHROPIC_API_KEY` n'est pas configuree.
- Mise a jour du script `test.py` pour envoyer `X-Webhook-Secret` depuis l'environnement local.
- Ajout d'une valeur locale `WEBHOOK_SECRET` dans `.env`.
- Remplacement de l'annotation `str | None` par `Optional[str]` pour rester compatible avec le Python de l'environnement virtuel local.

Verifications :

- Verification syntaxique effectuee avec `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py test.py`.
- Verification dans l'environnement virtuel du projet avec `/Users/thomasmahe/setter-agent/.venv/bin/python -m py_compile main.py test.py`.
- Verification FastAPI avec `TestClient` : appel sans secret = 401, appel avec mauvais secret = 401.

Limites et suites :

- Configurer `WEBHOOK_SECRET` dans Railway et dans le `.env` local.
- Configurer `WEBHOOK_SECRET` dans Railway avec la meme valeur que le `.env` local si l'outil appelant utilise ce secret.
- Configurer l'outil appelant le webhook pour envoyer le header `X-Webhook-Secret`.
- Ajouter ensuite une gestion d'erreurs autour de l'appel Anthropic.

### Creation du journal d'interventions

Contexte :

- Demande utilisateur : creer un fichier pour documenter tout ce que Codex fait dans ce projet.

Fichiers consultes :

- `main.py`
- `README.md`
- `test.py`
- `pyproject.toml`
- `requirements.txt`
- `railway.json`

Fichiers modifies :

- `AGENT_LOG.md`

Changements effectues :

- Ajout d'un fichier dedie au suivi des interventions Codex.
- Definition d'un format simple pour les futures entrees.
- Ajout de la premiere entree documentant la creation de ce journal.

Verifications :

- Aucune verification runtime necessaire, changement documentaire uniquement.

Limites et suites :

- Les prochaines interventions devront etre ajoutees dans ce fichier au fur et a mesure.

## 2026-05-10

### Socle backend des relances IA

Contexte :

- Demande utilisateur : connecter la page Relance avec le backend et commencer la generation de relances par l'agent.

Fichiers consultes :

- `main.py`
- `AGENT_LOG.md`
- `app/relance/page.tsx` et `lib/api.ts` dans `setter-dashboard-ttr`

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- Ajout de timestamps dans les nouveaux messages stockes dans `history`.
- Nettoyage des messages envoyes a Claude pour ne pas lui passer les metadonnees `timestamp`.
- Ajout de `GET /follow-ups/due` pour lister les relances dues selon les seuils 23 h, J+3 et J+10.
- Ajout de `POST /follow-ups/preview` pour generer une proposition de relance avec Claude.
- L'envoi automatique ManyChat n'est pas encore branche.

Verifications :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` dans `setter-dashboard-ttr`

Limites et suites :

- Les anciennes conversations sans timestamp utilisent `created_at` comme fallback.
- La prochaine etape sera de valider les propositions dans le dashboard, puis de brancher l'envoi auto uniquement avant 24 h.

### Options agent persistantes pour les liens

Contexte :

- Demande utilisateur : les liens Calendly et page de vente doivent etre des options de l'agent, pas seulement des liens du test playground.

Fichiers consultes :

- `main.py`
- `AGENT_LOG.md`
- `app/agent/page.tsx` dans `setter-dashboard-ttr`

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- Ajout des marqueurs internes `AGENT_OPTIONS_START` / `AGENT_OPTIONS_END` dans le prompt actif.
- Ajout de `GET /agent-links` pour lire les liens depuis le prompt actif.
- Ajout de `PATCH /agent-links` pour creer une nouvelle version active du prompt avec les liens agent.
- Conservation de l'injection playground pour que le test utilise immediatement les champs saisis.

Verifications :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` dans `setter-dashboard-ttr`

Limites et suites :

- Chaque sauvegarde des options cree une nouvelle entree `prompt_versions` avec `source=agent-options`, afin de garder l'historique et permettre une restauration.
- Restaurer une ancienne version de prompt peut restaurer d'anciens liens ou supprimer les options si cette version n'en contenait pas.

### Parametres de liens pour le playground agent

Contexte :

- Demande utilisateur : conserver la page Agent actuelle et ajouter uniquement le lien Calendly et le lien de la page de vente.

Fichiers consultes :

- `main.py`
- `AGENT_LOG.md`
- `app/agent/page.tsx` dans `setter-dashboard-ttr`

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- Ajout de champs optionnels `calendly_url` et `sales_page_url` au payload `POST /playground`.
- Injection de ces liens dans le system prompt uniquement pour le playground dashboard.
- Aucun changement sur `POST /webhook` ni sur le prompt actif sauvegarde.

Verifications :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` dans `setter-dashboard-ttr`

Limites et suites :

- Ces liens affectent les tests de la page Agent uniquement. Pour modifier le comportement live du webhook, il faudra appliquer une nouvelle version de prompt ou ajouter une configuration partagee.

### Chargement progressif des conversations dashboard

Contexte :

- Demande utilisateur : reduire les chargements longs du dashboard et ajouter des animations pour eviter l'impression de blocage.

Fichiers consultes :

- `main.py`
- `AGENT_LOG.md`
- Fichiers frontend dans `setter-dashboard-ttr`

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- Ajout de `GET /conversations/summary` pour renvoyer une liste legere sans historique complet.
- Ajout de `GET /conversations/{conversation_id}` pour charger le detail complet uniquement au clic sur un prospect.
- Conservation de `GET /conversations` pour compatibilite avec l'ancien comportement.

Verifications :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` dans `setter-dashboard-ttr`

Limites et suites :

- Deployer le backend avant ou en meme temps que le frontend, car le dashboard utilise le nouvel endpoint `/conversations/summary`.
- Tester sur le dashboard live que la liste charge plus vite et que l'historique s'affiche au clic.

### Alignement des endpoints dashboard conversations

Contexte :

- Demande utilisateur : tester et corriger si necessaire la coherence entre le dashboard et l'agent.
- Observation : le dashboard appelle les endpoints de statut et suppression avec l'id Supabase de la conversation.

Fichiers consultes :

- `main.py`
- `AGENT_LOG.md`
- `lib/api.ts` dans `setter-dashboard-ttr`

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- Modification de `PATCH /conversations/{...}/status` pour filtrer Supabase par `id`.
- Modification de `DELETE /conversations/{...}` pour filtrer Supabase par `id`.
- Les endpoints ManyChat `POST /webhook`, `POST /activate` et `POST /deactivate` n'ont pas ete modifies.

Verifications :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
- `npm run build` dans `setter-dashboard-ttr`

Limites et suites :

- Tester sur l'environnement live que le changement de statut et la suppression modifient bien les lignes Supabase attendues.
- Verifier separement l'alignement `username` / `subscriber_id` pour les liens Instagram du dashboard.

### Envoi manuel securise de la relance H23

Contexte :

- Demande utilisateur : s'occuper de la premiere relance automatique, celle avant la limite Instagram/ManyChat des 24 heures.

Fichiers consultes :

- `main.py`
- `AGENT_LOG.md`
- Fichiers frontend dans `setter-dashboard-ttr`

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`
- `app/relance/page.tsx` dans `setter-dashboard-ttr`
- `lib/api.ts` dans `setter-dashboard-ttr`

Changements effectues :

- Ajout de `POST /follow-ups/{conversation_id}/send-auto-23h`.
- L'endpoint verifie que la conversation est bien due en `auto_23h`, donc entre 23 h et 24 h apres le dernier message prospect.
- Generation du message de relance avec Claude via le prompt de relance existant.
- Envoi via l'API ManyChat `sendContent` avec `username` comme subscriber id ManyChat.
- Ajout du message envoye dans `history` avec `follow_up_stage=auto_23h` pour eviter un double envoi.
- La page `/relance` affiche maintenant un bouton `Envoyer H23` pour les relances automatiques dues.

Limites et suites :

- Ce build ajoute l'envoi H23 declenche depuis le dashboard, pas encore une tache cron autonome.

## 2026-05-20

### Stabilisation du login dashboard SaaS

Contexte :

- Demande utilisateur : corriger les problemes de login du dashboard SaaS et aligner le backend avec Supabase Auth.

Fichiers consultes :

- `main.py`
- `README.md`
- Fichiers frontend dans `setter-dashboard-saas`

Fichiers modifies :

- `main.py`
- `README.md`
- `AGENT_LOG.md`
- `proxy.ts`, `app/login/page.tsx`, `lib/api.ts`, `lib/config.ts`, `lib/supabase.ts`, `lib/supabase/client.ts`, `.env.example`, `migrations/frontend_env_vars.md` dans `setter-dashboard-saas`

Changements effectues :

- Remplacement du middleware Next vide du dashboard par un `proxy.ts` compatible Next 16.
- Protection serveur des routes dashboard et redirection automatique login/CRM selon la session Supabase.
- Nettoyage de la page login : redirection via router Next, suppression du log de resultat auth, trim email.
- Ajout d'erreurs explicites si `NEXT_PUBLIC_API_URL` ou Supabase Auth ne sont pas configures.
- Durcissement backend : refus des JWT qui ne correspondent pas a `OWNER_USER_ID` quand il est configure.
- Ajout de filtres `user_id` aux lectures et mutations de conversations du dashboard.
- Mise a jour des variables frontend documentees et de la doc backend sur l'auth dashboard.

Verifications :

- `npm run build` dans `setter-dashboard-saas`
- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py config.py prompts.py test.py`

Limites et suites :

- `npm run lint` reste bloque par une installation locale ESLint cassee dans `node_modules`, a reparer via reinstall des dependances si necessaire.
- Verifier sur Vercel que `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_SUPABASE_URL` et `NEXT_PUBLIC_SUPABASE_ANON_KEY` sont bien configurees.
- Prochaine etape possible : ajouter un cron Railway qui appelle l'envoi H23 automatiquement toutes les quelques minutes.

### Endpoint ManyChat pour relance J1

Contexte :

- Demande utilisateur : brancher la relance J1 directement dans ManyChat avec un External Request.
- ManyChat appelait `/follow-ups/manychat-auto-23h` et recevait `404 Not Found`.

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- Ajout de `POST /follow-ups/manychat-auto-23h`.
- Payload attendu : `{ "subscriber_id": "..." }`.
- Recherche de la conversation via `username = subscriber_id`.
- Verification que la relance due est bien `auto_23h`.
- Generation du message IA puis retour JSON `{ "message": "...", "conversation_id": "...", "stage": "auto_23h" }`.
- Marquage dans `history` avec `follow_up_stage=auto_23h` et `source=follow_up_manychat` pour eviter les doublons.

Verification :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`

Limite :

- Si le contact teste dans ManyChat n'est pas actuellement entre 23 h et 24 h depuis son dernier message, l'endpoint renverra `409 Auto 23h follow-up is not due`.

### Reponse stable pour mapping ManyChat

Contexte :

- ManyChat a besoin d'une reponse `200 OK` avec le champ `message` pour configurer le Response mapping.
- Avec un contact hors fenetre H23, l'endpoint renvoyait `409`, ce qui bloquait le mapping.

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- `POST /follow-ups/manychat-auto-23h` renvoie maintenant toujours un JSON stable.
- Cas non eligible : `{ "ok": false, "message": "", "reason": "..." }`.
- Cas eligible : `{ "ok": true, "message": "...", "conversation_id": "...", "stage": "auto_23h", "reason": null }`.

Verification :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`

Suite ManyChat :

- Mapper `message` vers `relance_j1_message`.
- Ajouter une condition `relance_j1_message is not empty` avant le bloc d'envoi Instagram.

### Mode test ManyChat relance J1

Contexte :

- Demande utilisateur : tester le flow ManyChat en temps reel sans attendre un prospect dans la fenetre H23.

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- Ajout de `force_test` et `test_only` au payload `POST /follow-ups/manychat-auto-23h`.
- `force_test=true` permet de generer une relance meme si la conversation n'est pas entre 23 h et 24 h.
- `test_only=true` evite de marquer la relance comme envoyee dans Supabase.
- La reponse indique `reason: "test_mode"` et `test_only: true` en mode test.

Verification :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`

Important :

- Retirer `force_test` et `test_only` du body ManyChat avant publication finale du flow.

### Retrait du mode test ManyChat relance J1

Contexte :

- Demande utilisateur : retirer le mode test apres validation du flow.

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- Suppression de `force_test` et `test_only` du payload ManyChat.
- `POST /follow-ups/manychat-auto-23h` ne genere plus de relance hors eligibilite H23.
- Le marquage Supabase redevient systematique quand une relance est generee.

Verification :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`

### Relances basees sur le prompt actif

Contexte :

- Demande utilisateur : utiliser une version hybride pour les relances, avec le prompt specialise relance + le metaprompt actif.

Fichiers modifies :

- `main.py`
- `AGENT_LOG.md`

Changements effectues :

- `generate_follow_up_message()` charge maintenant `get_active_prompt()`.
- Le prompt actif est injecte dans le contexte donne a Claude pour les relances.
- Le prompt systeme de relance reste specialise pour garder un message court, naturel et non agressif.

Verification :

- `PYTHONPYCACHEPREFIX=/private/tmp/codex-pycache python3 -m py_compile main.py`
