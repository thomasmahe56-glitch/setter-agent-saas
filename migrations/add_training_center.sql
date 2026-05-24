CREATE TABLE IF NOT EXISTS agent_profiles (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  profile JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_avatars (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  source_inputs JSONB NOT NULL DEFAULT '{}',
  avatar JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_sales_rules (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
  rules JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS agent_profiles_user_updated_idx
ON agent_profiles (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS agent_avatars_user_updated_idx
ON agent_avatars (user_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS agent_sales_rules_user_updated_idx
ON agent_sales_rules (user_id, updated_at DESC);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agent_profiles_set_updated_at ON agent_profiles;
CREATE TRIGGER agent_profiles_set_updated_at
BEFORE UPDATE ON agent_profiles
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS agent_avatars_set_updated_at ON agent_avatars;
CREATE TRIGGER agent_avatars_set_updated_at
BEFORE UPDATE ON agent_avatars
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS agent_sales_rules_set_updated_at ON agent_sales_rules;
CREATE TRIGGER agent_sales_rules_set_updated_at
BEFORE UPDATE ON agent_sales_rules
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

ALTER TABLE agent_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_avatars ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_sales_rules ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS agent_profiles_owner_select ON agent_profiles;
DROP POLICY IF EXISTS agent_profiles_owner_insert ON agent_profiles;
DROP POLICY IF EXISTS agent_profiles_owner_update ON agent_profiles;
DROP POLICY IF EXISTS agent_profiles_owner_delete ON agent_profiles;

CREATE POLICY agent_profiles_owner_select
ON agent_profiles FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY agent_profiles_owner_insert
ON agent_profiles FOR INSERT
WITH CHECK (user_id = auth.uid());

CREATE POLICY agent_profiles_owner_update
ON agent_profiles FOR UPDATE
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY agent_profiles_owner_delete
ON agent_profiles FOR DELETE
USING (user_id = auth.uid());

DROP POLICY IF EXISTS agent_avatars_owner_select ON agent_avatars;
DROP POLICY IF EXISTS agent_avatars_owner_insert ON agent_avatars;
DROP POLICY IF EXISTS agent_avatars_owner_update ON agent_avatars;
DROP POLICY IF EXISTS agent_avatars_owner_delete ON agent_avatars;

CREATE POLICY agent_avatars_owner_select
ON agent_avatars FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY agent_avatars_owner_insert
ON agent_avatars FOR INSERT
WITH CHECK (user_id = auth.uid());

CREATE POLICY agent_avatars_owner_update
ON agent_avatars FOR UPDATE
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY agent_avatars_owner_delete
ON agent_avatars FOR DELETE
USING (user_id = auth.uid());

DROP POLICY IF EXISTS agent_sales_rules_owner_select ON agent_sales_rules;
DROP POLICY IF EXISTS agent_sales_rules_owner_insert ON agent_sales_rules;
DROP POLICY IF EXISTS agent_sales_rules_owner_update ON agent_sales_rules;
DROP POLICY IF EXISTS agent_sales_rules_owner_delete ON agent_sales_rules;

CREATE POLICY agent_sales_rules_owner_select
ON agent_sales_rules FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY agent_sales_rules_owner_insert
ON agent_sales_rules FOR INSERT
WITH CHECK (user_id = auth.uid());

CREATE POLICY agent_sales_rules_owner_update
ON agent_sales_rules FOR UPDATE
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY agent_sales_rules_owner_delete
ON agent_sales_rules FOR DELETE
USING (user_id = auth.uid());
