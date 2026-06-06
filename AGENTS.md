## Feature: Automation Mode + Refine With Angelos
Deployed on May 19, 2026.

### Automation Mode
Each conversation has an `automation_mode` field (TEXT, default 'supervised'):
- `auto`: Angelos generates + sends directly through ManyChat (active 24h window)
- `supervised`: Angelos generates, stores in `pending_message`, Thomas sends manually
- `disabled`: incoming message is stored in history, nothing is generated

Columns added in the database (migration: migrations/add_automation_mode.sql):
- `automation_mode` TEXT DEFAULT 'supervised' CHECK (auto/supervised/disabled)
- `pending_message` TEXT
- `pending_message_at` TIMESTAMPTZ

Endpoints added in main.py:
- PATCH /conversations/{id}/automation-mode → changes the mode
- POST /conversations/{id}/ignore-pending → clears pending_message, marks ignored=True in history
- POST /conversations/{id}/refine-pending → Angelos refines the message (see below)

Each assistant message in the JSONB history now has these fields:
- `sent` (bool): True if sent through ManyChat, False if supervised and pending
- `ignored` (bool): True if Thomas ignored the message
- `edited` (bool): True if refined by Angelos on Thomas's instruction
- `generated_content`: original version before refinement
- `refinement_instruction`: instruction given by Thomas to Angelos

### Refine With Angelos
In the dashboard pending_message banner, button "✨ Ask Angelos to refine".
Thomas enters a natural-language instruction ("Make it warmer", "Too long, shorten it"...).
Angelos regenerates while taking the prospect's last 10 messages into account.
The pending_message is updated immediately in the banner.
The refined message is traced in history with edited=True + refinement_instruction.
→ Strong signal for the feedback loop: compare generated_content vs final content.

### Instagram 24h Constraint
The `auto` mode (direct ManyChat send) only works inside the active 24h window.
Follow-ups (D+3, D+10) are always supervised: Copy + ig.me/m/{display_name} link.

### Dashboard UI
- Auto / Supervised / Off selector in the ConversationPanel header
- Colored badge in ProspectList: green (auto), orange (supervised), gray (off)
- Pending banner: Copy, Open Instagram, Refine with Angelos, Ignore
