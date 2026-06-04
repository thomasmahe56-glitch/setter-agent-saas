ALTER TABLE prompt_versions
ADD COLUMN IF NOT EXISTS refinement_instruction TEXT,
ADD COLUMN IF NOT EXISTS refinement_applied_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS previous_version_id UUID REFERENCES prompt_versions(id) ON DELETE SET NULL,
ADD COLUMN IF NOT EXISTS prompt_diff JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS prompt_versions_refinement_applied_idx
ON prompt_versions (user_id, refinement_applied_at DESC)
WHERE refinement_applied_at IS NOT NULL;
