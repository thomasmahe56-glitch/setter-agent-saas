DO $$
DECLARE
  constraint_name text;
BEGIN
  FOR constraint_name IN
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'conversations'::regclass
      AND contype = 'u'
      AND pg_get_constraintdef(oid) = 'UNIQUE (username)'
  LOOP
    EXECUTE format('ALTER TABLE conversations DROP CONSTRAINT %I', constraint_name);
  END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS conversations_user_channel_external_idx
ON conversations (user_id, channel, external_contact_id);
