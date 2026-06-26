-- Backfill: replace unresolved ManyChat/template placeholder values in display_name
-- and username with a safe fallback so they no longer appear in the dashboard.
--
-- A placeholder looks like {{ig_username}}, {{username}}, {{first_name}}, etc.
-- Run this once in the Supabase SQL editor (or via the CLI).

-- 1. Update display_name when it contains a placeholder but username is a valid handle.
UPDATE conversations
SET display_name = username
WHERE
  display_name ~ '\{\{[^}]*\}\}'        -- display_name is a placeholder
  AND username IS NOT NULL
  AND username !~ '\{\{[^}]*\}\}'       -- username is not a placeholder
  AND username <> '';

-- 2. For rows where both display_name and username are placeholders (or either is still bad),
--    fall back to "Instagram prospect".
UPDATE conversations
SET display_name = 'Instagram prospect'
WHERE
  display_name ~ '\{\{[^}]*\}\}';

-- 3. Sanity-check: show any remaining rows that still have a placeholder (should be 0).
SELECT id, username, display_name
FROM conversations
WHERE
  display_name ~ '\{\{[^}]*\}\}'
  OR username   ~ '\{\{[^}]*\}\}';
