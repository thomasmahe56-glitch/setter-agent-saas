CREATE TABLE IF NOT EXISTS webhook_secrets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  secret TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS webhook_secrets_user_created_idx
ON webhook_secrets (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS webhook_secrets_secret_idx
ON webhook_secrets (secret);

ALTER TABLE webhook_secrets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS webhook_secrets_owner_select ON webhook_secrets;
DROP POLICY IF EXISTS webhook_secrets_owner_insert ON webhook_secrets;
DROP POLICY IF EXISTS webhook_secrets_owner_update ON webhook_secrets;
DROP POLICY IF EXISTS webhook_secrets_owner_delete ON webhook_secrets;

CREATE POLICY webhook_secrets_owner_select
ON webhook_secrets FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY webhook_secrets_owner_insert
ON webhook_secrets FOR INSERT
WITH CHECK (user_id = auth.uid());

CREATE POLICY webhook_secrets_owner_update
ON webhook_secrets FOR UPDATE
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY webhook_secrets_owner_delete
ON webhook_secrets FOR DELETE
USING (user_id = auth.uid());
