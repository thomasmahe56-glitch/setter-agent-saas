ALTER TABLE insights
ADD COLUMN IF NOT EXISTS user_id UUID;

ALTER TABLE prompt_versions
ADD COLUMN IF NOT EXISTS user_id UUID;

CREATE INDEX IF NOT EXISTS insights_user_created_idx
ON insights (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS prompt_versions_user_active_created_idx
ON prompt_versions (user_id, is_active, created_at DESC);

ALTER TABLE insights ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_versions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS insights_owner_select ON insights;
DROP POLICY IF EXISTS insights_owner_insert ON insights;
DROP POLICY IF EXISTS insights_owner_update ON insights;
DROP POLICY IF EXISTS insights_owner_delete ON insights;

CREATE POLICY insights_owner_select
ON insights FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY insights_owner_insert
ON insights FOR INSERT
WITH CHECK (user_id = auth.uid());

CREATE POLICY insights_owner_update
ON insights FOR UPDATE
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY insights_owner_delete
ON insights FOR DELETE
USING (user_id = auth.uid());

DROP POLICY IF EXISTS prompt_versions_owner_select ON prompt_versions;
DROP POLICY IF EXISTS prompt_versions_owner_insert ON prompt_versions;
DROP POLICY IF EXISTS prompt_versions_owner_update ON prompt_versions;
DROP POLICY IF EXISTS prompt_versions_owner_delete ON prompt_versions;

CREATE POLICY prompt_versions_owner_select
ON prompt_versions FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY prompt_versions_owner_insert
ON prompt_versions FOR INSERT
WITH CHECK (user_id = auth.uid());

CREATE POLICY prompt_versions_owner_update
ON prompt_versions FOR UPDATE
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY prompt_versions_owner_delete
ON prompt_versions FOR DELETE
USING (user_id = auth.uid());

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS conversations_owner_select ON conversations;
DROP POLICY IF EXISTS conversations_owner_insert ON conversations;
DROP POLICY IF EXISTS conversations_owner_update ON conversations;
DROP POLICY IF EXISTS conversations_owner_delete ON conversations;

CREATE POLICY conversations_owner_select
ON conversations FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY conversations_owner_insert
ON conversations FOR INSERT
WITH CHECK (user_id = auth.uid());

CREATE POLICY conversations_owner_update
ON conversations FOR UPDATE
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY conversations_owner_delete
ON conversations FOR DELETE
USING (user_id = auth.uid());
