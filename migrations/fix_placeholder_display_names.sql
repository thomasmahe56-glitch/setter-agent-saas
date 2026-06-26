-- Backfill: replace invalid display_name values with a safe fallback.
--
-- Invalid values are:
--   1. Unresolved ManyChat/template placeholders: {{ig_username}}, {{first_name}}, etc.
--   2. Raw numeric subscriber IDs: 612751574, 1168444660, etc. (6+ digit pure numbers)
--
-- Run once in the Supabase SQL editor.

-- 1. Where display_name is invalid but username looks like a real IG handle, use username.
UPDATE conversations
SET display_name = username
WHERE
  (
    display_name ~ '\{\{[^}]*\}\}'   -- placeholder pattern
    OR display_name ~ '^\d{6,}$'    -- pure numeric subscriber ID
  )
  AND username IS NOT NULL
  AND username !~ '\{\{[^}]*\}\}'   -- username is not a placeholder
  AND username !~ '^\d{6,}$'        -- username is not a numeric ID
  AND username <> '';

-- 2. Anything still invalid falls back to "Instagram prospect".
UPDATE conversations
SET display_name = 'Instagram prospect'
WHERE
  display_name ~ '\{\{[^}]*\}\}'
  OR display_name ~ '^\d{6,}$';

-- 3. Sanity-check: should return 0 rows.
SELECT id, username, display_name
FROM conversations
WHERE
  display_name ~ '\{\{[^}]*\}\}'
  OR display_name ~ '^\d{6,}$'
  OR username    ~ '\{\{[^}]*\}\}';
