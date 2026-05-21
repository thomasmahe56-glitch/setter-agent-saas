ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS channel TEXT DEFAULT 'instagram'
  CHECK (channel IN ('instagram', 'whatsapp'));

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS external_contact_id TEXT;

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS phone_e164 TEXT;

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS last_inbound_at TIMESTAMPTZ;

ALTER TABLE conversations
ADD COLUMN IF NOT EXISTS transport_metadata JSONB DEFAULT '{}';

UPDATE conversations
SET
  channel = COALESCE(channel, 'instagram'),
  external_contact_id = COALESCE(external_contact_id, username),
  last_inbound_at = COALESCE(last_inbound_at, created_at),
  transport_metadata = COALESCE(transport_metadata, '{}'::jsonb)
WHERE external_contact_id IS NULL
   OR channel IS NULL
   OR last_inbound_at IS NULL
   OR transport_metadata IS NULL;

ALTER TABLE conversations
ALTER COLUMN channel SET DEFAULT 'instagram';

ALTER TABLE conversations
ALTER COLUMN external_contact_id SET NOT NULL;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'conversations_username_key'
      AND conrelid = 'conversations'::regclass
  ) THEN
    ALTER TABLE conversations DROP CONSTRAINT conversations_username_key;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS conversations_user_channel_external_idx
ON conversations (user_id, channel, external_contact_id);
