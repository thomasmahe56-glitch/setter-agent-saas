ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS automation_mode TEXT DEFAULT 'supervised'
  CHECK (automation_mode IN ('auto', 'supervised', 'disabled'));

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS pending_message TEXT;

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS pending_message_at TIMESTAMPTZ;
