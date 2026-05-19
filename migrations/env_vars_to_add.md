## Variables à ajouter dans Railway (setter-agent-saas)

### Auth JWT

```
SUPABASE_JWT_SECRET=<à récupérer dans Supabase → Settings → API → JWT Settings → JWT Secret>
OWNER_USER_ID=7b8b8a81-a22d-49ab-8564-e159db5546e2
```

### Comment récupérer SUPABASE_JWT_SECRET

1. Ouvre ton projet sur https://supabase.com
2. Settings (icône engrenage) → API
3. Section "JWT Settings" → copie "JWT Secret"
4. Colle la valeur dans Railway → Variables → SUPABASE_JWT_SECRET

### Notes

- OWNER_USER_ID = l'UUID Supabase Auth du coach propriétaire de ce compte
  (récupérable dans Supabase → Authentication → Users)
- SUPABASE_JWT_SECRET est utilisé pour valider les tokens JWT émis par Supabase Auth
- DASHBOARD_SECRET reste nécessaire si tu l'utilises encore localement (optionnel)
