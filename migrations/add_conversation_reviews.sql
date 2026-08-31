CREATE TABLE IF NOT EXISTS conversation_reviews (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  review_date DATE NOT NULL,
  conversation_updated_at TIMESTAMPTZ,
  username TEXT NOT NULL DEFAULT 'unknown',
  message_count INTEGER NOT NULL DEFAULT 0,
  objective_reached BOOLEAN NOT NULL DEFAULT false,
  objective_reason TEXT NOT NULL DEFAULT '',
  human_likeness_score INTEGER NOT NULL CHECK (human_likeness_score BETWEEN 1 AND 10),
  sales_effectiveness_score INTEGER NOT NULL CHECK (sales_effectiveness_score BETWEEN 1 AND 10),
  engagement_score INTEGER NOT NULL CHECK (engagement_score BETWEEN 1 AND 10),
  moment_of_failure TEXT NOT NULL DEFAULT '',
  failure_category TEXT NOT NULL DEFAULT 'other',
  what_angellos_did_wrong TEXT NOT NULL DEFAULT '',
  better_human_reply TEXT NOT NULL DEFAULT '',
  lesson_learned TEXT NOT NULL DEFAULT '',
  prompt_rule_candidate TEXT NOT NULL DEFAULT '',
  lesson_status TEXT NOT NULL DEFAULT 'candidate'
    CHECK (lesson_status IN ('candidate', 'approved', 'rejected', 'ignored')),
  approved_at TIMESTAMPTZ,
  reviewer_model TEXT NOT NULL DEFAULT '',
  raw_review JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, conversation_id, review_date)
);

CREATE INDEX IF NOT EXISTS conversation_reviews_user_date_idx
ON conversation_reviews (user_id, review_date DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS conversation_reviews_user_status_idx
ON conversation_reviews (user_id, lesson_status, created_at DESC);

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS conversation_reviews_set_updated_at ON conversation_reviews;
CREATE TRIGGER conversation_reviews_set_updated_at
BEFORE UPDATE ON conversation_reviews
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();

ALTER TABLE conversation_reviews ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS conversation_reviews_owner_select ON conversation_reviews;
DROP POLICY IF EXISTS conversation_reviews_owner_insert ON conversation_reviews;
DROP POLICY IF EXISTS conversation_reviews_owner_update ON conversation_reviews;
DROP POLICY IF EXISTS conversation_reviews_owner_delete ON conversation_reviews;

CREATE POLICY conversation_reviews_owner_select
ON conversation_reviews FOR SELECT
USING (user_id = auth.uid());

CREATE POLICY conversation_reviews_owner_insert
ON conversation_reviews FOR INSERT
WITH CHECK (user_id = auth.uid());

CREATE POLICY conversation_reviews_owner_update
ON conversation_reviews FOR UPDATE
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY conversation_reviews_owner_delete
ON conversation_reviews FOR DELETE
USING (user_id = auth.uid());
