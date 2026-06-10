from fastapi import FastAPI, Header, HTTPException, Depends, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from anthropic import Anthropic
from dotenv import load_dotenv
from config import load_config
from prompts import build_system_prompt, build_analysis_prompt, build_follow_up_prompt
import hmac
import httpx
import hashlib
import difflib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

load_dotenv()
config = load_config()

ANTHROPIC_API_KEY = config.anthropic_api_key
DASHBOARD_SECRET = config.dashboard_secret
SUPABASE_SERVICE_KEY = config.supabase_key
SUPABASE_PROJECT_URL = config.supabase_url.replace("/rest/v1", "").rstrip("/")
SUPABASE_AUTH_USER_URL = f"{SUPABASE_PROJECT_URL}/auth/v1/user"
SUPABASE_CONVERSATIONS_URL = f"{config.supabase_url}/conversations"
SUPABASE_WEBHOOK_SECRETS_URL = f"{config.supabase_url}/webhook_secrets"
SUPABASE_INSIGHTS_URL = f"{config.supabase_url}/insights"
SUPABASE_PROMPT_VERSIONS_URL = f"{config.supabase_url}/prompt_versions"
SUPABASE_AGENT_PROFILES_URL = f"{config.supabase_url}/agent_profiles"
SUPABASE_AGENT_AVATARS_URL = f"{config.supabase_url}/agent_avatars"
SUPABASE_AGENT_SALES_RULES_URL = f"{config.supabase_url}/agent_sales_rules"
MANYCHAT_API_KEY = config.manychat_token
MANYCHAT_SEND_URL = "https://api.manychat.com/fb/sending/sendContent"
WHATSAPP_ACCESS_TOKEN = config.whatsapp_access_token
WHATSAPP_PHONE_NUMBER_ID = config.whatsapp_phone_number_id
WHATSAPP_VERIFY_TOKEN = config.whatsapp_verify_token
META_APP_SECRET = config.meta_app_secret
GRAPH_API_VERSION = config.graph_api_version or "v23.0"
WHATSAPP_SEND_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
MAX_HISTORY_TURNS = 40
AGENT_OPTIONS_START = "<!-- AGENT_OPTIONS_START -->"
AGENT_OPTIONS_END = "<!-- AGENT_OPTIONS_END -->"
AGENT_PROFILE_START = "<!-- AGENT_PROFILE_START -->"
AGENT_PROFILE_END = "<!-- AGENT_PROFILE_END -->"
AGENT_PROFILE_PROMPT_START = "<!-- AGENT_PROFILE_PROMPT_START -->"
AGENT_PROFILE_PROMPT_END = "<!-- AGENT_PROFILE_PROMPT_END -->"
TRAINING_CENTER_START = "<!-- TRAINING_CENTER_START -->"
TRAINING_CENTER_END = "<!-- TRAINING_CENTER_END -->"
TRAINING_CENTER_PROMPT_START = "<!-- TRAINING_CENTER_PROMPT_START -->"
TRAINING_CENTER_PROMPT_END = "<!-- TRAINING_CENTER_PROMPT_END -->"
AUTO_FOLLOW_UP_HOURS = 23
MANUAL_FOLLOW_UP_1_HOURS = 72
MANUAL_FOLLOW_UP_2_HOURS = 240
ANGELLOS_REJECTION_REPLY = "No worries, appreciate you getting back to me."
ANGELLOS_WHAT_IS_REPLY = (
    "Angellos is an AI setter for Instagram DMs. It helps qualify conversations, handle replies and follow-ups, "
    "and move serious prospects toward booked calls."
)
ANGELLOS_HOW_IT_WORKS_REPLY = (
    "You connect your Instagram DM flow through ManyChat. Angellos uses your offer, qualification criteria, "
    "FAQs and tone of voice to handle the first part of the conversation. It does not replace you completely. "
    "It filters the noise, qualifies serious people, and pushes the right ones toward a call."
)
ANGELLOS_PRICE_REPLY = (
    "For the beta, it’s free for 30 days. I’m looking for feedback, screenshots and proof that it can help "
    "operators turn more DM conversations into calls. If it works well, we can talk about the paid version later."
)
ANGELLOS_INTERESTED_REPLY = (
    "Best next step is a quick call so I can see your current DM flow and check if you’re a good fit for the beta. "
    "I’m only taking 3 people because I want to set it up properly and follow the results closely. "
    "Want me to send the beta page?"
)
ANGELLOS_OUTBOUND_REPLY = (
    "Actually that’s still relevant. If you start the conversation manually, Angellos can help once they reply. "
    "It can handle the next messages, qualify whether they’re a real fit, follow up if needed, and move the right "
    "people toward a call. Once people reply to your outreach, do you already have a consistent qualification "
    "process or do you handle it manually every time?"
)
ANGELLOS_INBOUND_REPLY = (
    "Perfect. Then Angellos can help with the first part of the conversation: qualifying people, answering common "
    "questions, following up, and moving serious prospects toward a call."
)
ANGELLOS_BETA_REPLY_RULES = f"""=== ANGELLOS BETA MARKET SETTINGS ===
Market: English-speaking beta
Default language: English
Brand name: Angellos

These rules override older base prompts, fallback prompts, tenant configuration and examples.
- Angellos helps operators turn Instagram DM conversations into booked calls, whether the conversation starts inbound or from manual outbound.
- Do not say Angellos is only for people who get inbound messages.
- If the prospect says they mostly do outbound, segment the use case and explain that Angellos helps once people reply to manual outreach.
- If the prospect says they reach out through Instagram DMs, understand that Angellos can still help with reply handling, qualification, follow-up, objection handling and moving qualified prospects toward a call.
- If the prospect says they get inbound DMs, explain the inbound use case briefly.
- Write in English by default.
- Use French only when the prospect writes in French first.
- Always spell the brand Angellos with two Ls.
- Do not use emojis unless the prospect used emojis first.
- Keep the tone human, direct and casual.
- No corporate tone.
- No hype language.
- Never use dashes in generated messages.
- Ask one question at a time.
- Keep replies short.
- Do not mention API fees unless asked about setup costs.
- Do not overqualify too early.
- If the prospect rejects the offer with wording like "No thanks", "not interested", "nah" or "no thanks bro", reply exactly: {ANGELLOS_REJECTION_REPLY}
- If the prospect says they mostly do outbound or reach out through Instagram DMs, reply exactly: {ANGELLOS_OUTBOUND_REPLY}
- If the prospect says they get inbound DMs, reply exactly: {ANGELLOS_INBOUND_REPLY}
- If the prospect asks what Angellos is or what Angellos does, reply exactly: {ANGELLOS_WHAT_IS_REPLY}
- If the prospect asks how it works, reply exactly: {ANGELLOS_HOW_IT_WORKS_REPLY}
- If the prospect asks about price, reply exactly: {ANGELLOS_PRICE_REPLY}
- If the prospect shows interest, move toward a quick call instead of over-explaining in DMs. Default reply: {ANGELLOS_INTERESTED_REPLY}
- New beta outreach conversations stay supervised unless Thomas explicitly activates auto mode."""

client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

app = FastAPI()

def cors_allowed_origins() -> list[str]:
    origins = [
        origin.strip().rstrip("/")
        for origin in (config.cors_allowed_origins or "").split(",")
        if origin.strip()
    ]
    default_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://setter-dashboard-saas.vercel.app",
    ]
    return list(dict.fromkeys([*origins, *default_origins]))


app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)



def supabase_headers() -> dict:
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey": SUPABASE_SERVICE_KEY,
        "Content-Type": "application/json",
    }


async def require_secret(x_webhook_secret: Optional[str]) -> str:
    secret = (x_webhook_secret or "").strip()
    print(f"[require_secret] received_secret_prefix={secret[:10]!r}")
    print(f"[require_secret] supabase_url={SUPABASE_WEBHOOK_SECRETS_URL}")
    if not secret:
        raise HTTPException(status_code=401, detail="Missing webhook secret")
    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_WEBHOOK_SECRETS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={
                "secret": f"eq.{secret}",
                "select": "user_id",
                "limit": "1",
            },
            timeout=10.0,
        )
    print(f"[require_secret] supabase_status={res.status_code}")
    print(f"[require_secret] supabase_body={res.text}")
    if res.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Supabase webhook secret lookup error: {res.text[:200]}")
    rows = res.json()
    user_id = rows[0].get("user_id") if rows else None
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    return user_id


require_webhook_user_id = require_secret


def verify_meta_signature(body: bytes, x_hub_signature_256: Optional[str]) -> None:
    if not META_APP_SECRET:
        raise HTTPException(status_code=500, detail="META_APP_SECRET is not configured")
    if not x_hub_signature_256 or not x_hub_signature_256.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing Meta signature")
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid Meta signature")


def owner_scope(user_id: Optional[str] = None) -> dict:
    return {"user_id": f"eq.{user_id}"} if user_id else {}


def row_owner_fields(user_id: Optional[str] = None) -> dict:
    return {"user_id": user_id} if user_id else {}


def require_dashboard_secret(x_dashboard_secret: Optional[str]) -> None:
    if not DASHBOARD_SECRET:
        raise HTTPException(status_code=500, detail="DASHBOARD_SECRET is not configured")
    if not x_dashboard_secret or not hmac.compare_digest(x_dashboard_secret, DASHBOARD_SECRET):
        raise HTTPException(status_code=401, detail="Invalid dashboard secret")


async def require_jwt(authorization: Optional[str] = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    if not SUPABASE_PROJECT_URL:
        raise HTTPException(status_code=500, detail="SUPABASE_URL is not configured")
    if not SUPABASE_SERVICE_KEY:
        raise HTTPException(status_code=500, detail="SUPABASE_KEY is not configured")

    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_AUTH_USER_URL,
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {token}",
            },
            timeout=10.0,
        )
    if res.status_code == 401:
        raise HTTPException(status_code=401, detail=f"Invalid Supabase session: {res.text[:200]}")
    if res.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Supabase Auth error: {res.status_code} {res.text[:200]}")

    user = res.json()
    user_id = user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid Supabase session: no user id")
    return user_id


def extract_agent_links(prompt: str) -> dict:
    block_match = re.search(
        rf"{re.escape(AGENT_OPTIONS_START)}(.*?){re.escape(AGENT_OPTIONS_END)}",
        prompt,
        flags=re.DOTALL,
    )
    block = block_match.group(1) if block_match else ""
    calendly_match = re.search(r"(?:Calendly link|Lien Calendly)\s*:\s*(\S+)", block)
    sales_page_match = re.search(r"(?:Sales page link|Lien page de vente)\s*:\s*(\S+)", block)
    return {
        "calendly_url": calendly_match.group(1).strip() if calendly_match else "",
        "sales_page_url": sales_page_match.group(1).strip() if sales_page_match else "",
    }


def strip_agent_options(prompt: str) -> str:
    return re.sub(
        rf"\n*\s*{re.escape(AGENT_OPTIONS_START)}.*?{re.escape(AGENT_OPTIONS_END)}\s*",
        "\n",
        prompt,
        flags=re.DOTALL,
    ).strip()


def extract_agent_profile(prompt: str) -> dict:
    block_match = re.search(
        rf"{re.escape(AGENT_PROFILE_START)}(.*?){re.escape(AGENT_PROFILE_END)}",
        prompt,
        flags=re.DOTALL,
    )
    if not block_match:
        return {}
    block = block_match.group(1).strip()
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return {}


def strip_agent_profile(prompt: str) -> str:
    prompt = re.sub(
        rf"\n*\s*{re.escape(AGENT_PROFILE_START)}.*?{re.escape(AGENT_PROFILE_END)}\s*",
        "\n",
        prompt,
        flags=re.DOTALL,
    )
    return re.sub(
        rf"\n*\s*{re.escape(AGENT_PROFILE_PROMPT_START)}.*?{re.escape(AGENT_PROFILE_PROMPT_END)}\s*",
        "\n",
        prompt,
        flags=re.DOTALL,
    ).strip()


def strip_training_center(prompt: str) -> str:
    prompt = re.sub(
        rf"\n*\s*{re.escape(TRAINING_CENTER_START)}.*?{re.escape(TRAINING_CENTER_END)}\s*",
        "\n",
        prompt,
        flags=re.DOTALL,
    )
    return re.sub(
        rf"\n*\s*{re.escape(TRAINING_CENTER_PROMPT_START)}.*?{re.escape(TRAINING_CENTER_PROMPT_END)}\s*",
        "\n",
        prompt,
        flags=re.DOTALL,
    ).strip()


def append_agent_profile(prompt: str, profile: dict) -> str:
    prompt = strip_agent_profile(prompt)
    clean_profile = {
        key: (value.strip() if isinstance(value, str) else value)
        for key, value in profile.items()
        if value is not None and (not isinstance(value, str) or value.strip())
    }
    if not clean_profile:
        return prompt
    profile_json = json.dumps(clean_profile, ensure_ascii=False, indent=2)
    profile_prompt = format_agent_profile_for_prompt(clean_profile)
    return (
        f"{prompt}\n\n{AGENT_PROFILE_START}\n{profile_json}\n{AGENT_PROFILE_END}\n\n"
        f"{AGENT_PROFILE_PROMPT_START}\n"
        f"=== ANGELOS BUSINESS PROFILE AND VOICE ===\n{profile_prompt}\n"
        f"{AGENT_PROFILE_PROMPT_END}"
    )


def clean_json_value(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return [clean_json_value(item) for item in value if clean_json_value(item) not in ("", None, [], {})]
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, val in value.items()
            if (cleaned := clean_json_value(val)) not in ("", None, [], {})
        }
    return value


def parse_llm_json(raw: str) -> dict:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("Claude returned JSON but not an object")
    return parsed


def build_prompt_diff(before: str, after: str) -> list[dict]:
    diff = difflib.ndiff(before.splitlines(), after.splitlines())
    visual_diff = []
    for line in diff:
        marker = line[:2]
        text = line[2:]
        if marker == "- ":
            visual_diff.append({"type": "remove", "line": text})
        elif marker == "+ ":
            visual_diff.append({"type": "add", "line": text})
        elif marker == "  " and visual_diff and len(visual_diff) < 120:
            visual_diff.append({"type": "keep", "line": text})
    return visual_diff[:160]


def normalize_prompt_refinement_result(raw_result: dict, current_prompt: str) -> dict:
    updated_prompt = (raw_result.get("updated_prompt") or raw_result.get("prompt_updated") or "").strip()
    if not updated_prompt:
        raise ValueError("Claude did not return updated_prompt")
    if updated_prompt == current_prompt.strip():
        raise ValueError("Claude returned the same prompt")
    if len(updated_prompt) < max(200, int(len(current_prompt) * 0.4)):
        raise ValueError("Claude returned a prompt that is suspiciously short")
    return {
        "updated_prompt": updated_prompt,
        "target_section": str(raw_result.get("target_section") or "section ciblee").strip(),
        "summary": str(raw_result.get("summary") or "").strip(),
        "changes": raw_result.get("changes") if isinstance(raw_result.get("changes"), list) else [],
    }


def prompt_refinement_source(instruction: str) -> str:
    clean_instruction = re.sub(r"\s+", " ", instruction).strip()
    if len(clean_instruction) > 72:
        clean_instruction = f"{clean_instruction[:69]}..."
    return f"training-refine: {clean_instruction}"


def format_training_center_for_prompt(profile: dict, avatar: dict, sales_rules: dict) -> str:
    parts = [
        "=== TRAINING CENTER ANGELOS ===",
        "This structured data is the business source of truth. Apply it before generic prompt examples.",
    ]
    if profile:
        parts.extend([
            "",
            "STRUCTURED BUSINESS PROFILE:",
            json.dumps(profile, ensure_ascii=False, indent=2),
        ])
    if avatar:
        parts.extend([
            "",
            "STRUCTURED CLIENT AVATAR:",
            json.dumps(avatar, ensure_ascii=False, indent=2),
        ])
    if sales_rules:
        parts.extend([
            "",
            "STRUCTURED DM RULES:",
            json.dumps(sales_rules, ensure_ascii=False, indent=2),
        ])
    parts.extend([
        "",
        "Execution priorities:",
        "1. Respect stop_conditions, red_flags, bad_fit, and do_not_say.",
        "2. Qualify with qualification_questions and detect buying_signals without interrogating.",
        "3. Use exact_words and objections to speak like the prospect, without copying mechanically.",
        "4. Offer a call or page only when call_offer_conditions are met.",
    ])
    return "\n".join(parts)


def build_training_center_prompt(base_prompt: str, profile: dict, avatar: dict, sales_rules: dict) -> str:
    clean_prompt = strip_training_center(base_prompt)
    payload = {
        "agent_profile": clean_json_value(profile or {}),
        "agent_avatar": clean_json_value(avatar or {}),
        "agent_sales_rules": clean_json_value(sales_rules or {}),
    }
    prompt_block = format_training_center_for_prompt(
        payload["agent_profile"],
        payload["agent_avatar"],
        payload["agent_sales_rules"],
    )
    return (
        f"{clean_prompt}\n\n{TRAINING_CENTER_START}\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        f"{TRAINING_CENTER_END}\n\n{TRAINING_CENTER_PROMPT_START}\n"
        f"{prompt_block}\n{TRAINING_CENTER_PROMPT_END}"
    )


def format_agent_profile_for_prompt(profile: dict) -> str:
    labels = {
        "avatar_client": "Client avatar",
        "offer": "Offer",
        "price": "Price and terms",
        "pain_points": "Pain points and frustrations",
        "goals": "Prospect goals",
        "objections": "Frequent objections",
        "qualification_rules": "Qualification questions and rules",
        "sales_rules": "Sales rules",
        "proof_points": "Proof points, results, and client cases",
        "voice_samples": "Transcripts, posts, or examples of the coach's voice",
        "tone_rules": "Voice style to mimic",
        "forbidden_phrases": "Words or phrasing to avoid",
    }
    lines = []
    for key, label in labels.items():
        value = profile.get(key)
        if value:
            lines.append(f"{label} :\n{value}")
    lines.append(
        "Use this context to answer like the coach: precise, natural, human, "
        "adapted to the offer and style. Do not recite this information; transform it "
        "into short, useful answers in the Instagram conversation."
    )
    return "\n\n".join(lines)


def append_agent_options(prompt: str, calendly_url: str = "", sales_page_url: str = "") -> str:
    prompt = strip_agent_options(prompt)
    lines = [
        AGENT_OPTIONS_START,
        "OPTIONS AGENT DASHBOARD :",
    ]
    if calendly_url:
        lines.append(f"Calendly link: {calendly_url}")
    if sales_page_url:
        lines.append(f"Sales page link: {sales_page_url}")
    if calendly_url or sales_page_url:
        lines.append("These links replace call and sales page links present elsewhere in the prompt.")
    lines.append(AGENT_OPTIONS_END)
    return f"{prompt}\n\n" + "\n".join(lines)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def strip_message_metadata(messages: list) -> list:
    return [
        {"role": msg.get("role"), "content": msg.get("content", "")}
        for msg in messages
        if msg.get("role") in {"user", "assistant"}
    ]


def build_angellos_beta_prompt(base_prompt: str) -> str:
    return f"{base_prompt}\n\n{ANGELLOS_BETA_REPLY_RULES}"


def normalize_inbound_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())).strip()


def get_angellos_beta_canned_reply(message: str) -> Optional[tuple[str, bool]]:
    normalized = normalize_inbound_text(message)
    if not normalized:
        return None

    rejection_phrases = {
        "no",
        "no thanks",
        "no thank you",
        "no thanks bro",
        "nah",
        "not interested",
        "im not interested",
        "i am not interested",
    }
    if (
        normalized in rejection_phrases
        or normalized.startswith("not interested ")
        or normalized.startswith("no thanks ")
        or normalized.startswith("no thank you ")
    ):
        return ANGELLOS_REJECTION_REPLY, True

    outbound_terms = (
        "outbound",
        "reach out",
        "reaching out",
        "outreach",
        "cold dm",
        "cold dms",
        "manual dm",
        "manual dms",
        "message people",
        "dm people",
    )
    instagram_dm_terms = (
        "instagram dm",
        "instagram dms",
        "ig dm",
        "ig dms",
        "dms",
    )
    if any(term in normalized for term in outbound_terms) and any(term in normalized for term in instagram_dm_terms):
        return ANGELLOS_OUTBOUND_REPLY, False
    if "mostly do outbound" in normalized or "mostly outbound" in normalized:
        return ANGELLOS_OUTBOUND_REPLY, False

    inbound_terms = (
        "inbound dm",
        "inbound dms",
        "get inbound",
        "getting inbound",
        "people dm me",
        "people message me",
        "they dm me",
        "they message me",
    )
    if any(term in normalized for term in inbound_terms):
        return ANGELLOS_INBOUND_REPLY, False

    if re.search(r"\b(what is|what s|whats|what does)\b.*\bangellos\b", normalized):
        return ANGELLOS_WHAT_IS_REPLY, False
    if re.search(r"\bhow\b.*\b(work|works|working)\b", normalized):
        return ANGELLOS_HOW_IT_WORKS_REPLY, False
    if re.search(r"\b(price|pricing|cost|costs|paid|free|how much)\b", normalized):
        return ANGELLOS_PRICE_REPLY, False
    if re.search(r"\b(interested|sounds good|send it|send me|beta page|tell me more|let s do it|lets do it)\b", normalized):
        return ANGELLOS_INTERESTED_REPLY, False

    return None


def text_has_emoji(text: str) -> bool:
    return bool(re.search(r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]", text or ""))


def sanitize_angellos_beta_reply(reply: str, prospect_message: str) -> str:
    cleaned = re.sub(r"[—–-]", " ", reply or "")
    if not text_has_emoji(prospect_message):
        cleaned = re.sub(r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]", "", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


def is_whatsapp_test_contact(conversation: dict) -> bool:
    metadata = conversation.get("transport_metadata") or {}
    return metadata.get("phone_number_id") == WHATSAPP_PHONE_NUMBER_ID


def is_whatsapp_test_metadata(metadata: Optional[dict]) -> bool:
    return bool(metadata and metadata.get("phone_number_id") == WHATSAPP_PHONE_NUMBER_ID)


def get_message_time(message: dict) -> Optional[datetime]:
    return parse_iso(message.get("timestamp") or message.get("created_at"))


def conversation_activity(conversation: dict) -> dict:
    history = conversation.get("history") or []
    last_user_at = None
    last_agent_at = None

    for message in history:
        timestamp = get_message_time(message)
        if message.get("role") == "user" and timestamp:
            last_user_at = timestamp
        if message.get("role") == "assistant" and timestamp:
            last_agent_at = timestamp

    fallback = parse_iso(conversation.get("created_at"))
    if last_user_at is None:
        last_user_at = fallback

    return {"last_user_at": last_user_at, "last_agent_at": last_agent_at}


def get_follow_up_stage(hours_since_user: float) -> Optional[dict]:
    if AUTO_FOLLOW_UP_HOURS <= hours_since_user < 24:
        return {"stage": "auto_23h", "label": "Auto 23 h", "mode": "auto", "sort": 1}
    if MANUAL_FOLLOW_UP_1_HOURS <= hours_since_user < MANUAL_FOLLOW_UP_2_HOURS:
        return {"stage": "j3", "label": "J+3", "mode": "manual", "sort": 2}
    if hours_since_user >= MANUAL_FOLLOW_UP_2_HOURS:
        return {"stage": "j10", "label": "J+10", "mode": "manual", "sort": 3}
    return None


def has_follow_up_stage(history: list, stage: str) -> bool:
    return any(message.get("follow_up_stage") == stage for message in history)


def build_follow_up_item(conversation: dict) -> Optional[dict]:
    if not conversation.get("agent_active"):
        return None
    if conversation.get("automation_mode") == "disabled":
        return None
    if conversation.get("status") in {"appel_booke", "signe"}:
        return None

    history = conversation.get("history") or []
    activity = conversation_activity(conversation)
    last_user_at = activity["last_user_at"]
    last_agent_at = activity["last_agent_at"]
    if not last_user_at:
        return None
    if last_agent_at and last_user_at > last_agent_at:
        return None

    now = datetime.now(timezone.utc)
    hours_since_user = (now - last_user_at).total_seconds() / 3600
    stage = get_follow_up_stage(hours_since_user)
    if not stage:
        return None
    if has_follow_up_stage(history, stage["stage"]):
        return None

    return {
        "conversation_id": conversation.get("id"),
        "id": conversation.get("id"),
        "created_at": conversation.get("created_at"),
        "username": conversation.get("username"),
        "channel": conversation.get("channel") or "instagram",
        "external_contact_id": conversation.get("external_contact_id") or conversation.get("username"),
        "phone_e164": conversation.get("phone_e164"),
        "display_name": conversation.get("display_name"),
        "message": conversation.get("message"),
        "status": conversation.get("status"),
        "agent_active": conversation.get("agent_active"),
        "automation_mode": conversation.get("automation_mode") or "supervised",
        "manual_contact_url": manual_contact_url(conversation),
        "stage": stage["stage"],
        "stage_label": stage["label"],
        "mode": stage["mode"],
        "sort": stage["sort"],
        "hours_since_user": round(hours_since_user, 1),
        "last_user_message_at": last_user_at.isoformat(),
        "last_agent_message_at": last_agent_at.isoformat() if last_agent_at else None,
    }




async def get_active_prompt(user_id: Optional[str] = None) -> str:
    """Return the active prompt from prompt_versions, or the hardcoded fallback."""
    if not user_id:
        return build_system_prompt(config)
    try:
        async with httpx.AsyncClient() as http:
            params = {
                "is_active": "eq.true",
                "order": "created_at.desc",
                "limit": "1",
                **owner_scope(user_id),
            }
            res = await http.get(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params=params,
                timeout=5.0,
            )
            res.raise_for_status()
            rows = res.json()
            if rows and rows[0].get("content"):
                return rows[0]["content"]
    except Exception as e:
        print(f"[get_active_prompt] fallback to hardcoded prompt: {e}")
    return build_system_prompt(config)


async def get_active_prompt_version(user_id: str) -> dict:
    """Return the full active version if it exists, otherwise a non-persisted fallback."""
    try:
        async with httpx.AsyncClient() as http:
            res = await http.get(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={
                    "is_active": "eq.true",
                    "order": "created_at.desc",
                    "limit": "1",
                    "select": "*",
                    **owner_scope(user_id),
                },
                timeout=5.0,
            )
            res.raise_for_status()
            rows = res.json()
            if rows and rows[0].get("content"):
                return rows[0]
    except Exception as e:
        print(f"[get_active_prompt_version] fallback to hardcoded prompt: {e}")
    return {
        "id": None,
        "content": build_system_prompt(config),
        "source": "fallback",
        "is_active": True,
    }


async def get_contact(username: str, user_id: Optional[str] = None) -> Optional[dict]:
    return await get_contact_by_external_id(username, "instagram", user_id)


async def get_contact_by_external_id(
    external_contact_id: str,
    channel: str = "instagram",
    user_id: Optional[str] = None,
) -> Optional[dict]:
    if not user_id:
        return None
    if channel == "instagram":
        params = {"username": f"eq.{external_contact_id}", "limit": 1}
    else:
        params = {
            "channel": f"eq.{channel}",
            "external_contact_id": f"eq.{external_contact_id}",
            "limit": 1,
        }
    params["user_id"] = f"eq.{user_id}"
    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={**params, "order": "created_at.desc"},
        )
        res.raise_for_status()
        rows = res.json()
    return rows[0] if rows else None


async def create_contact(
    external_contact_id: str,
    display_name: str,
    message: str,
    channel: str,
    received_at: str,
    user_id: str,
    phone_e164: Optional[str] = None,
    transport_metadata: Optional[dict] = None,
) -> dict:
    row = {
        "username": external_contact_id,
        "display_name": display_name,
        "message": message,
        "status": "nouveau",
        "agent_active": True,
        "history": [],
        "channel": channel,
        "external_contact_id": external_contact_id,
        "phone_e164": phone_e164,
        "last_inbound_at": received_at,
        "transport_metadata": transport_metadata or {},
        "user_id": user_id,
    }
    async with httpx.AsyncClient() as http:
        res = await http.post(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=representation"},
            json=row,
            timeout=10.0,
        )
        if res.status_code >= 400:
            print(f"[create_contact] Supabase error status={res.status_code} body={res.text!r}")
            raise HTTPException(status_code=502, detail=f"Supabase create_contact error: {res.text[:500]}")
        rows = res.json()
    return rows[0] if rows else row


async def get_conversation_by_id(conversation_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    params = {"id": f"eq.{conversation_id}", "limit": "1"}
    if user_id:
        params["user_id"] = f"eq.{user_id}"
    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params=params,
        )
        res.raise_for_status()
        rows = res.json()
    return rows[0] if rows else None


async def require_owned_insight(insight_id: str, user_id: str) -> dict:
    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_INSIGHTS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={
                "id": f"eq.{insight_id}",
                **owner_scope(user_id),
                "select": "id,status",
                "limit": "1",
            },
            timeout=10.0,
        )
        res.raise_for_status()
        rows = res.json()
    if not rows:
        raise HTTPException(status_code=404, detail="Insight not found")
    return rows[0]


async def require_owned_prompt_version(version_id: str, user_id: str) -> dict:
    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_PROMPT_VERSIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={
                "id": f"eq.{version_id}",
                **owner_scope(user_id),
                "select": "id",
                "limit": "1",
            },
            timeout=10.0,
        )
        res.raise_for_status()
        rows = res.json()
    if not rows:
        raise HTTPException(status_code=404, detail="Prompt version not found")
    return rows[0]


async def get_user_singleton_row(table_url: str, user_id: str, select: str = "*") -> Optional[dict]:
    async with httpx.AsyncClient() as http:
        res = await http.get(
            table_url,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={
                "user_id": f"eq.{user_id}",
                "select": select,
                "limit": "1",
            },
            timeout=10.0,
        )
        res.raise_for_status()
        rows = res.json()
    return rows[0] if rows else None


async def upsert_user_singleton_row(table_url: str, user_id: str, payload: dict) -> dict:
    row = {**payload, **row_owner_fields(user_id)}
    async with httpx.AsyncClient() as http:
        res = await http.post(
            table_url,
            headers={
                **supabase_headers(),
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
            params={"on_conflict": "user_id"},
            json=row,
            timeout=10.0,
        )
        res.raise_for_status()
        created = res.json()
    return created[0] if isinstance(created, list) and created else created


def generate_claude_reply(messages: list, system_prompt: str = "") -> str:
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=system_prompt,
        messages=messages,
    )
    return response.content[0].text


async def send_manychat_message(subscriber_id: str, text: str) -> dict:
    async with httpx.AsyncClient() as http:
        res = await http.post(
            MANYCHAT_SEND_URL,
            headers={
                "Authorization": f"Bearer {MANYCHAT_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "subscriber_id": subscriber_id,
                "data": {
                    "version": "v2",
                    "content": {
                        "messages": [{"type": "text", "text": text}],
                    },
                },
            },
        )
    print(f"[manychat] status={res.status_code} body_len={len(res.text)}")
    return {"status_code": res.status_code, "body": res.text}


async def clear_manychat_agent_response(subscriber_id: str) -> None:
    if not MANYCHAT_API_KEY:
        return
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                "https://api.manychat.com/fb/subscriber/setCustomFieldByName",
                headers={"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"},
                json={"subscriber_id": subscriber_id, "field_name": "agent_response", "field_value": ""},
                timeout=10.0,
            )
    except Exception:
        pass


async def send_whatsapp_text(phone_e164: str, text: str) -> dict:
    if not WHATSAPP_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="WHATSAPP_ACCESS_TOKEN is not configured")
    if not WHATSAPP_PHONE_NUMBER_ID:
        raise HTTPException(status_code=500, detail="WHATSAPP_PHONE_NUMBER_ID is not configured")

    async with httpx.AsyncClient() as http:
        res = await http.post(
            WHATSAPP_SEND_URL,
            headers={
                "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": phone_e164,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
            timeout=15.0,
        )
    print(f"[whatsapp] status={res.status_code} body_len={len(res.text)}")
    return {"status_code": res.status_code, "body": res.text}


async def send_channel_message(conversation: dict, text: str) -> dict:
    channel = conversation.get("channel") or "instagram"
    if channel == "whatsapp":
        phone_e164 = conversation.get("phone_e164") or conversation.get("external_contact_id") or conversation.get("username")
        if not phone_e164:
            raise HTTPException(status_code=422, detail="Conversation has no WhatsApp phone number")
        return await send_whatsapp_text(phone_e164, text)

    subscriber_id = conversation.get("external_contact_id") or conversation.get("username")
    if not subscriber_id:
        raise HTTPException(status_code=422, detail="Conversation has no ManyChat subscriber id")
    return await send_manychat_message(subscriber_id, text)


def manual_contact_url(conversation: dict) -> Optional[str]:
    channel = conversation.get("channel") or "instagram"
    if channel == "whatsapp":
        phone = conversation.get("phone_e164") or conversation.get("external_contact_id")
        return f"https://wa.me/{phone.lstrip('+')}" if phone else None
    display_name = conversation.get("display_name") or conversation.get("username")
    return f"https://ig.me/m/{display_name}" if display_name else None


def allowed_reply_urls(system_prompt: str) -> set[str]:
    links = extract_agent_links(system_prompt)
    return {
        url.rstrip("/")
        for url in [
            config.url_call,
            config.url_page,
            links.get("calendly_url", ""),
            links.get("sales_page_url", ""),
        ]
        if url
    }


def extract_urls(text: str) -> list[str]:
    return [match.rstrip(".,;:!?)\"'") for match in re.findall(r"https?://\S+", text or "")]


def validate_agent_reply(reply: str, system_prompt: str) -> str:
    cleaned = (reply or "").strip()
    if not cleaned:
        raise HTTPException(status_code=502, detail="Empty AI reply")
    if len(cleaned) > 2000:
        raise HTTPException(status_code=502, detail="AI reply is too long")
    allowed_urls = allowed_reply_urls(system_prompt)
    unexpected_urls = [
        url for url in extract_urls(cleaned)
        if url.rstrip("/") not in allowed_urls
    ]
    if unexpected_urls:
        raise HTTPException(status_code=502, detail="AI reply contains an unauthorized URL")
    return cleaned


def split_stop_agent_reply(reply: str) -> tuple[str, bool]:
    if "[STOP_AGENT]" not in (reply or ""):
        return reply, False
    before_tag = reply.split("[STOP_AGENT]", 1)[0].strip()
    if len(before_tag) < 3:
        return "", True
    return before_tag, True


def has_processed_transport_message(history: list, transport_metadata: Optional[dict]) -> bool:
    message_id = (transport_metadata or {}).get("message_id")
    if not message_id:
        return False
    return any(
        ((message.get("transport_metadata") or {}).get("message_id") == message_id)
        for message in history
        if message.get("role") == "user"
    )


def mark_last_auto_assistant_sent(history: list, sent: bool, send_result: Optional[dict] = None) -> list:
    updated_history = []
    patched = False
    for message in reversed(history):
        if not patched and message.get("role") == "assistant" and message.get("source") == "inbound_auto":
            updated = dict(message)
            updated["sent"] = sent
            if send_result is not None:
                updated["send_status_code"] = send_result.get("status_code")
            updated_history.insert(0, updated)
            patched = True
        else:
            updated_history.insert(0, message)
    return updated_history


async def generate_follow_up_message(conversation: dict, stage: str) -> str:
    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    stage_labels = {
        "auto_23h": "automatic 23-hour follow-up",
        "j3": "assisted D+3 follow-up",
        "j10": "assisted D+10 follow-up",
    }
    stage_label = stage_labels.get(stage, stage)
    active_prompt = await get_active_prompt(conversation.get("user_id"))
    generation_prompt = build_angellos_beta_prompt(active_prompt)
    context = format_conversations_for_analysis([conversation], generation_prompt)
    user_message = (
        f"Follow-up stage: {stage_label}\n"
        f"Prospect: {conversation.get('display_name') or conversation.get('username')}\n"
        f"Last known message: {conversation.get('message', '')}\n\n"
        f"Respect the active prompt as the general framework, but write only a short follow-up adapted to the stage.\n\n"
        f"{context}\n\n"
        f"Generate the best follow-up for this stage."
    )

    try:
        reply = generate_claude_reply(
            [{"role": "user", "content": user_message}],
            build_angellos_beta_prompt(build_follow_up_prompt(config)),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {e}")

    reply = sanitize_angellos_beta_reply(reply, conversation.get("message", ""))
    return validate_agent_reply(reply, generation_prompt)


def format_conversations_for_analysis(conversations: list, system_prompt: str) -> str:
    parts = [
        "=== ACTIVE PROMPT ===",
        system_prompt,
        "",
        "=== CONVERSATIONS ===",
    ]
    for i, conv in enumerate(conversations, 1):
        history = conv.get("history") or []
        name = conv.get("display_name") or conv.get("username", "unknown")
        parts.append(f"\n--- Conversation {i} ---")
        parts.append(f"Prospect: {name}")
        parts.append(f"Status: {conv.get('status', 'unknown')}")
        parts.append(f"Messages ({len(history)}):")
        for msg in history:
            role = "Prospect" if msg.get("role") == "user" else "Agent"
            content = msg.get("content", "")
            parts.append(f"  [{role}] : {content}")
    return "\n".join(parts)


# ── Pydantic models ────────────────────────────────────────────────────────────

class WebhookPayload(BaseModel):
    username: str = Field(max_length=200)
    message: str = Field(max_length=4000)
    subscriber_id: str = Field(max_length=200)


class AgentControlPayload(BaseModel):
    username: str
    subscriber_id: Optional[str] = None


class StatusPayload(BaseModel):
    status: str


class FeedbackLoopPayload(BaseModel):
    n: int = 20  # number of conversations to analyze (max 50)
    manual_observations: Optional[str] = None
    test_conversation: Optional[str] = None


class PreviewPromptPayload(BaseModel):
    insight_id: str
    selected_suggestions: list[str] = []
    selected_pain_points: list[str] = []
    selected_objections: list[str] = []


class ApplyPromptPayload(BaseModel):
    insight_id: str
    prompt_proposed: str


class RefinePromptPayload(BaseModel):
    instruction: str = Field(max_length=1200)
    active_prompt: Optional[str] = Field(default=None, max_length=120000)
    prompt_proposed: Optional[str] = Field(default=None, max_length=120000)
    apply: bool = False


class AgentLinksPayload(BaseModel):
    calendly_url: str = ""
    sales_page_url: str = ""


class AgentProfilePayload(BaseModel):
    avatar_client: str = ""
    offer: str = ""
    price: str = ""
    pain_points: str = ""
    goals: str = ""
    objections: str = ""
    qualification_rules: str = ""
    sales_rules: str = ""
    proof_points: str = ""
    voice_samples: str = ""
    tone_rules: str = ""
    forbidden_phrases: str = ""


class TrainingProfilePayload(BaseModel):
    business_name: str = Field(default="", max_length=300)
    coach_name: str = Field(default="", max_length=300)
    niche: str = Field(default="", max_length=1000)
    offer_name: str = Field(default="", max_length=300)
    offer_promise: str = Field(default="", max_length=2000)
    offer_format: str = Field(default="", max_length=2000)
    price: str = Field(default="", max_length=1000)
    proof_points: list[str] = Field(default_factory=list)
    tone_rules: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    calendly_url: str = Field(default="", max_length=1000)
    sales_page_url: str = Field(default="", max_length=1000)
    raw_notes: str = Field(default="", max_length=8000)


class AvatarGeneratePayload(BaseModel):
    client_ideal: str = Field(default="", max_length=2000)
    main_problem: str = Field(default="", max_length=2000)
    current_block: str = Field(default="", max_length=2000)
    fears: str = Field(default="", max_length=2000)
    tried_before: str = Field(default="", max_length=2000)
    buying_hesitations: str = Field(default="", max_length=2000)
    desired_outcome: str = Field(default="", max_length=2000)
    bad_fit: str = Field(default="", max_length=2000)


class AvatarSavePayload(BaseModel):
    source_inputs: AvatarGeneratePayload = Field(default_factory=AvatarGeneratePayload)
    avatar: dict


class AvatarDataPayload(BaseModel):
    avatar: dict


class SalesRulesGeneratePayload(BaseModel):
    avatar: Optional[dict] = None
    profile: Optional[dict] = None


class SalesRulesSavePayload(BaseModel):
    rules: dict


class FollowUpPreviewPayload(BaseModel):
    conversation_id: str
    stage: str


class ManyChatFollowUpPayload(BaseModel):
    subscriber_id: str


class AutomationModePayload(BaseModel):
    automation_mode: str  # "auto" | "supervised" | "disabled"


class RefineMessagePayload(BaseModel):
    instruction: str = Field(max_length=1000)
    original_message: str = Field(default="", max_length=4000)


class PlaygroundPayload(BaseModel):
    messages: list  # list of {role: str, content: str}
    calendly_url: Optional[str] = None
    sales_page_url: Optional[str] = None


async def handle_inbound_message(
    *,
    channel: str,
    external_contact_id: str,
    display_name: str,
    message: str,
    user_id: str,
    phone_e164: Optional[str] = None,
    transport_metadata: Optional[dict] = None,
    auto_send_transport: bool = True,
) -> dict:
    received_at = now_iso()
    contact = await get_contact_by_external_id(external_contact_id, channel, user_id)
    if contact is None:
        contact = await create_contact(
            external_contact_id=external_contact_id,
            display_name=display_name,
            message=message,
            channel=channel,
            received_at=received_at,
            user_id=user_id,
            phone_e164=phone_e164,
            transport_metadata=transport_metadata,
        )

    history = contact.get("history") or []
    if has_processed_transport_message(history, transport_metadata):
        print(f"[inbound] DUPLICATE channel={channel} external_id={external_contact_id}")
        return {
            "reply": "",
            "sent": False,
            "should_send": False,
            "mode": contact.get("automation_mode") or "supervised",
            "skipped": True,
            "reason": "duplicate_message",
            "conversation_id": contact.get("id"),
        }

    user_message = {
        "role": "user",
        "content": message,
        "timestamp": received_at,
        "channel": channel,
    }
    if transport_metadata:
        user_message["transport_metadata"] = transport_metadata
    messages = history + [user_message]
    if len(messages) > MAX_HISTORY_TURNS * 2:
        messages = messages[-(MAX_HISTORY_TURNS * 2):]

    patch_data: dict = {
        "message": message,
        "history": messages,
        "last_inbound_at": received_at,
    }
    if display_name:
        patch_data["display_name"] = display_name
    if phone_e164:
        patch_data["phone_e164"] = phone_e164
    if transport_metadata:
        patch_data["transport_metadata"] = transport_metadata

    automation_mode = contact.get("automation_mode") or "supervised"
    if automation_mode == "disabled":
        patch_data["pending_message"] = None
        patch_data["pending_message_at"] = None
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_CONVERSATIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{contact.get('id')}", "user_id": f"eq.{user_id}"},
                json=patch_data,
                timeout=10.0,
            )
            res.raise_for_status()
        print(f"[inbound] DISABLED channel={channel} external_id={external_contact_id}")
        return {
            "reply": "",
            "sent": False,
            "should_send": False,
            "mode": "disabled",
            "skipped": True,
            "reason": "automation_disabled",
            "conversation_id": contact.get("id"),
        }

    active_prompt = await get_active_prompt(contact.get("user_id"))
    system_prompt = build_angellos_beta_prompt(active_prompt)
    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")
    if not contact.get("agent_active"):
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_CONVERSATIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{contact.get('id')}", "user_id": f"eq.{user_id}"},
                json=patch_data,
                timeout=10.0,
            )
            res.raise_for_status()
        if channel == "instagram":
            await clear_manychat_agent_response(external_contact_id)
        print(f"[inbound] INACTIVE_HISTORY_ONLY channel={channel} external_id={external_contact_id}")
        return {
            "reply": "",
            "sent": False,
            "should_send": False,
            "mode": automation_mode,
            "skipped": True,
            "reason": "agent_inactive",
            "conversation_id": contact.get("id"),
        }

    canned_reply = get_angellos_beta_canned_reply(message)
    if canned_reply:
        reply, should_stop_agent = canned_reply
    else:
        first_turn = not history
        prospect_label = "Prospect WhatsApp" if channel == "whatsapp" else "Prospect Instagram"
        user_content = (
            f"{prospect_label}: {display_name}\nReceived message: {message}"
            if first_turn
            else message
        )
        messages_for_generation = history + [{"role": "user", "content": user_content, "timestamp": received_at}]

        try:
            reply = generate_claude_reply(strip_message_metadata(messages_for_generation), system_prompt)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Anthropic API error: {e}")
        reply, should_stop_agent = split_stop_agent_reply(reply)
    reply = sanitize_angellos_beta_reply(reply, message)
    reply = validate_agent_reply(reply, system_prompt)

    if should_stop_agent:
        if not (channel == "whatsapp" and (is_whatsapp_test_contact(contact) or is_whatsapp_test_metadata(transport_metadata))):
            patch_data["agent_active"] = False
            print(f"[inbound] STOP_AGENT channel={channel} external_id={external_contact_id}")
        else:
            print(f"[inbound] STOP_AGENT ignored for whatsapp test contact external_id={external_contact_id}")

    should_send = automation_mode == "auto"
    delegated_to_webhook_sender = should_send and not auto_send_transport
    sent = False
    assistant_entry = {
        "role": "assistant",
        "content": reply,
        "timestamp": now_iso(),
        "channel": channel,
        "sent": delegated_to_webhook_sender,
        "ignored": False,
        "source": "inbound_auto" if should_send else "inbound_supervised",
    }
    if delegated_to_webhook_sender:
        assistant_entry["send_transport"] = "manychat_webhook_response"
        assistant_entry["send_status_code"] = 202
    new_history = messages + [assistant_entry]
    if len(new_history) > MAX_HISTORY_TURNS * 2:
        new_history = new_history[-(MAX_HISTORY_TURNS * 2):]

    patch_data.update({
        "response": reply,
        "history": new_history,
        "status": "en_cours",
    })
    if automation_mode == "supervised":
        patch_data["pending_message"] = reply
        patch_data["pending_message_at"] = now_iso()
    elif automation_mode == "auto":
        patch_data["pending_message"] = None
        patch_data["pending_message_at"] = None

    if automation_mode == "auto" and auto_send_transport:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_CONVERSATIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{contact.get('id')}", "user_id": f"eq.{user_id}"},
                json=patch_data,
                timeout=10.0,
            )
            res.raise_for_status()

        send_result = await send_channel_message(contact, reply)
        sent = send_result["status_code"] < 400
        sent_history = mark_last_auto_assistant_sent(new_history, send_result["status_code"] < 400, send_result)
        followup_patch = {
            "history": sent_history,
            "pending_message": None if send_result["status_code"] < 400 else reply,
            "pending_message_at": None if send_result["status_code"] < 400 else now_iso(),
        }
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_CONVERSATIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{contact.get('id')}", "user_id": f"eq.{user_id}"},
                json=followup_patch,
                timeout=10.0,
            )
            res.raise_for_status()
        if send_result["status_code"] >= 400:
            raise HTTPException(status_code=502, detail=f"{channel} send error")
    else:
        send_result = {"status_code": 202, "body": "delegated_to_webhook_sender"} if delegated_to_webhook_sender else None
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_CONVERSATIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{contact.get('id')}", "user_id": f"eq.{user_id}"},
                json=patch_data,
                timeout=10.0,
            )
            res.raise_for_status()

    print(f"[inbound] REPLY channel={channel} external_id={external_contact_id} mode={automation_mode}")
    return {
        "reply": reply,
        "sent": sent,
        "should_send": should_send,
        "mode": automation_mode,
        "skipped": False,
        "reason": None,
        "send_result": send_result,
        "conversation_id": contact.get("id"),
    }


# ── Webhooks ──────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(
    payload: WebhookPayload,
    x_webhook_secret: Optional[str] = Header(default=None),
):
    user_id = await require_secret(x_webhook_secret)

    result = await handle_inbound_message(
        channel="instagram",
        external_contact_id=payload.subscriber_id,
        display_name=payload.username,
        message=payload.message,
        user_id=user_id,
        transport_metadata={"provider": "manychat"},
        auto_send_transport=False,
    )
    should_send = bool(result.get("should_send"))
    mode = result.get("mode") or "supervised"
    public_mode = "off" if mode == "disabled" else mode
    reply = result.get("reply") or ""
    return {
        "agent_response": reply if should_send else "",
        "suggested_response": reply if mode == "supervised" else "",
        "should_send": should_send,
        "mode": public_mode,
        "automation_mode": mode,
        "reason": result.get("reason"),
    }


@app.get("/webhooks/whatsapp")
async def verify_whatsapp_webhook(
    hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge"),
    hub_mode_fallback: Optional[str] = Query(default=None, alias="hub_mode"),
    hub_verify_token_fallback: Optional[str] = Query(default=None, alias="hub_verify_token"),
    hub_challenge_fallback: Optional[str] = Query(default=None, alias="hub_challenge"),
):
    mode = (hub_mode or hub_mode_fallback or "").strip()
    verify_token = (hub_verify_token or hub_verify_token_fallback or "").strip()
    expected_token = (WHATSAPP_VERIFY_TOKEN or "").strip()
    challenge = hub_challenge or hub_challenge_fallback or ""
    if mode == "subscribe" and expected_token and hmac.compare_digest(verify_token, expected_token):
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Invalid WhatsApp verify token")


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(default=None),
    x_webhook_secret: Optional[str] = Header(default=None),
):
    body = await request.body()
    verify_meta_signature(body, x_hub_signature_256)
    user_id = await require_secret(x_webhook_secret)
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    processed = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            contacts = {contact.get("wa_id"): contact for contact in value.get("contacts", [])}
            for message_item in value.get("messages", []):
                if message_item.get("type") != "text":
                    continue
                wa_id = message_item.get("from")
                text = ((message_item.get("text") or {}).get("body") or "").strip()
                if not wa_id or not text:
                    continue
                if len(text) > 4000:
                    print(f"[whatsapp] ignored oversized message from={wa_id} len={len(text)}")
                    continue
                contact = contacts.get(wa_id) or {}
                profile = contact.get("profile") or {}
                await handle_inbound_message(
                    channel="whatsapp",
                    external_contact_id=wa_id,
                    display_name=profile.get("name") or wa_id,
                    message=text,
                    user_id=user_id,
                    phone_e164=wa_id,
                    transport_metadata={
                        "provider": "meta_whatsapp_cloud_api",
                        "message_id": message_item.get("id"),
                        "phone_number_id": (value.get("metadata") or {}).get("phone_number_id"),
                    },
                )
                processed += 1

    return {"success": True, "processed": processed}


# ── Dashboard endpoints ────────────────────────────────────────────────────────

@app.get("/auth/me")
async def auth_me(
    user_id: str = Depends(require_jwt),
):
    return {
        "user_id": user_id,
        "owner_user_id_configured": bool(config.owner_user_id),
        "matches_owner_user_id": (not config.owner_user_id) or hmac.compare_digest(user_id, config.owner_user_id),
    }


@app.get("/conversations")
async def get_conversations(
    user_id: str = Depends(require_jwt),
):

    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={"order": "created_at.desc", "limit": 500, "user_id": f"eq.{user_id}"},
        )
        res.raise_for_status()
        return res.json()


@app.get("/conversations/summary")
async def get_conversation_summaries(
    user_id: str = Depends(require_jwt),
):

    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={
                "order": "created_at.desc",
                "limit": 500,
                "user_id": f"eq.{user_id}",
                "select": "id,created_at,username,display_name,message,status,agent_active,automation_mode,pending_message,pending_message_at,channel,external_contact_id,phone_e164,last_inbound_at",
            },
            timeout=10.0,
        )
        res.raise_for_status()
        return res.json()


@app.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    user_id: str = Depends(require_jwt),
):

    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={"id": f"eq.{conversation_id}", "limit": "1", "user_id": f"eq.{user_id}"},
            timeout=10.0,
        )
        res.raise_for_status()
        rows = res.json()
    if not rows:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return rows[0]


@app.get("/debug/whatsapp-conversations/{external_contact_id}")
async def debug_whatsapp_conversations(
    external_contact_id: str,
    user_id: str = Depends(require_jwt),
):
    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={
                "channel": "eq.whatsapp",
                "external_contact_id": f"eq.{external_contact_id}",
                "user_id": f"eq.{user_id}",
                "select": "id,created_at,user_id,username,display_name,message,status,agent_active,automation_mode,pending_message,pending_message_at,last_inbound_at,history",
                "order": "created_at.desc",
            },
            timeout=10.0,
        )
        res.raise_for_status()
        rows = res.json()

    return [
        {
            "id": row.get("id"),
            "created_at": row.get("created_at"),
            "user_id": row.get("user_id"),
            "visible_to_current_user": row.get("user_id") == user_id,
            "username": row.get("username"),
            "display_name": row.get("display_name"),
            "message": row.get("message"),
            "status": row.get("status"),
            "agent_active": row.get("agent_active"),
            "automation_mode": row.get("automation_mode"),
            "pending_message": row.get("pending_message"),
            "pending_message_at": row.get("pending_message_at"),
            "last_inbound_at": row.get("last_inbound_at"),
            "history_count": len(row.get("history") or []),
            "history_tail": (row.get("history") or [])[-5:],
        }
        for row in rows
    ]


@app.post("/activate")
async def activate(
    payload: AgentControlPayload,
    user_id: str = Depends(require_jwt),
):

    contact = await get_contact(payload.username, user_id)
    history = (contact.get("history") or []) if contact else []
    patch_data: dict = {"agent_active": True}
    if not history:
        patch_data["pending_opener"] = True
    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"username": f"eq.{payload.username}", "user_id": f"eq.{user_id}"},
            json=patch_data,
        )
        res.raise_for_status()
    return {"status": "activated", "username": payload.username}


@app.post("/deactivate")
async def deactivate(
    payload: AgentControlPayload,
    user_id: str = Depends(require_jwt),
):

    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"username": f"eq.{payload.username}", "user_id": f"eq.{user_id}"},
            json={"agent_active": False},
        )
        res.raise_for_status()
    try:
        async with httpx.AsyncClient() as http:
            await http.post(
                "https://api.manychat.com/fb/subscriber/setCustomFieldByName",
                headers={"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"},
                json={"subscriber_id": payload.username, "field_name": "agent_response", "field_value": ""},
            )
    except Exception:
        pass
    return {"status": "deactivated", "username": payload.username}


@app.post("/conversations/{conversation_id}/activate")
async def activate_conversation(
    conversation_id: str,
    user_id: str = Depends(require_jwt),
):

    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}", "select": "history", "limit": "1"},
            timeout=5.0,
        )
        res.raise_for_status()
        rows = res.json()
    history = (rows[0].get("history") or []) if rows else []
    patch_data: dict = {"agent_active": True}
    if not history:
        patch_data["pending_opener"] = True
    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
            json=patch_data,
        )
        res.raise_for_status()
    return {"success": True}


@app.post("/conversations/{conversation_id}/deactivate")
async def deactivate_conversation(
    conversation_id: str,
    user_id: str = Depends(require_jwt),
):

    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
            json={"agent_active": False},
        )
        res.raise_for_status()
    return {"success": True}


@app.patch("/conversations/{conversation_id}/status")
async def update_status(
    conversation_id: str,
    payload: StatusPayload,
    user_id: str = Depends(require_jwt),
):

    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
            json={"status": payload.status},
        )
        res.raise_for_status()
    return {"status": "updated"}


@app.patch("/conversations/{conversation_id}/automation-mode")
async def update_automation_mode(
    conversation_id: str,
    payload: AutomationModePayload,
    user_id: str = Depends(require_jwt),
):

    if payload.automation_mode not in ("auto", "supervised", "disabled"):
        raise HTTPException(status_code=400, detail="Invalid automation_mode")
    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
            json={"automation_mode": payload.automation_mode},
        )
        res.raise_for_status()
    return {"success": True}


@app.post("/conversations/{conversation_id}/ignore-pending")
async def ignore_pending(
    conversation_id: str,
    user_id: str = Depends(require_jwt),
):

    conversation = await get_conversation_by_id(conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    history = conversation.get("history") or []
    updated = False
    for msg in reversed(history):
        if msg.get("role") == "assistant" and not msg.get("sent") and not msg.get("ignored"):
            msg["ignored"] = True
            updated = True
            break

    patch_data: dict = {
        "pending_message": None,
        "pending_message_at": None,
    }
    if updated:
        patch_data["history"] = history

    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
            json=patch_data,
        )
        res.raise_for_status()
    return {"success": True}


@app.post("/conversations/{conversation_id}/refine-pending")
async def refine_pending(
    conversation_id: str,
    payload: RefineMessagePayload,
    user_id: str = Depends(require_jwt),
):

    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    conversation = await get_conversation_by_id(conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    history = conversation.get("history") or []
    display_name = conversation.get("display_name") or conversation.get("username", "the prospect")
    active_prompt = await get_active_prompt(user_id)
    generation_prompt = build_angellos_beta_prompt(active_prompt)
    original_message = (conversation.get("pending_message") or "").strip()
    if not original_message:
        for msg in reversed(history):
            if msg.get("role") == "assistant" and not msg.get("sent") and not msg.get("ignored"):
                original_message = (msg.get("content") or "").strip()
                break
    if not original_message:
        raise HTTPException(status_code=409, detail="No pending message to refine")
    instruction = payload.instruction.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Refinement instruction is required")
    if len(instruction) > 1000:
        raise HTTPException(status_code=413, detail="Refinement instruction is too long")

    history_text = "\n".join([
        f"{'Prospect' if m.get('role') == 'user' else 'Angellos'}: {m.get('content', '')}"
        for m in history[-10:]
    ])

    refine_prompt = (
        f"You are Angellos, the Instagram setter agent for the English-speaking beta market.\n"
        f"Apply the Angellos beta rules exactly, including English by default, no emojis unless the prospect used them first, "
        f"no corporate tone, no hype language and no dashes.\n"
        f"You generated this message for prospect @{display_name}:\n"
        f"<message_original>\n{original_message}\n</message_original>\n\n"
        f"Here is the recent conversation context:\n"
        f"<history>\n{history_text}\n</history>\n\n"
        f"Thomas asks you to refine the message with this instruction:\n"
        f"<instruction>\n{instruction}\n</instruction>\n\n"
        f"Rewrite only the refined message, with no explanation, no quotation marks, "
        f"and no introduction. Just the final message exactly as it will be sent."
    )

    try:
        refined = generate_claude_reply(
            [{"role": "user", "content": refine_prompt}],
            generation_prompt,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {e}")

    last_prospect_message = next(
        ((msg.get("content") or "") for msg in reversed(history) if msg.get("role") == "user"),
        "",
    )
    refined = sanitize_angellos_beta_reply(refined, last_prospect_message)
    refined = validate_agent_reply(refined, generation_prompt)

    updated_history = []
    patched = False
    for msg in reversed(history):
        if not patched and msg.get("role") == "assistant" and not msg.get("sent") and not msg.get("ignored"):
            updated_msg = dict(msg)
            updated_msg["generated_content"] = original_message
            updated_msg["content"] = refined
            updated_msg["edited"] = True
            updated_msg["refinement_instruction"] = instruction
            updated_history.insert(0, updated_msg)
            patched = True
        else:
            updated_history.insert(0, msg)

    patch_data: dict = {
        "pending_message": refined,
        "pending_message_at": now_iso(),
    }
    if patched:
        patch_data["history"] = updated_history

    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
            json=patch_data,
        )
        res.raise_for_status()

    print(f"[refine-pending] conversation_id={conversation_id} instruction_len={len(instruction)}")
    return {"refined_message": refined}


@app.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    user_id: str = Depends(require_jwt),
):

    async with httpx.AsyncClient() as http:
        res = await http.delete(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
        )
        res.raise_for_status()
    return {"status": "deleted"}


# ── Follow-up endpoints ───────────────────────────────────────────────────────

@app.get("/follow-ups/due")
async def get_due_follow_ups(
    user_id: str = Depends(require_jwt),
):

    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={
                "order": "created_at.desc",
                "limit": "500",
                "user_id": f"eq.{user_id}",
                "select": "id,created_at,username,display_name,message,status,agent_active,automation_mode,history,channel,external_contact_id,phone_e164,last_inbound_at",
            },
            timeout=10.0,
        )
        res.raise_for_status()
        conversations = res.json()

    items = [item for conv in conversations if (item := build_follow_up_item(conv))]
    items.sort(key=lambda item: (item["sort"], -item["hours_since_user"]))
    return items


@app.post("/follow-ups/preview")
async def preview_follow_up(
    payload: FollowUpPreviewPayload,
    user_id: str = Depends(require_jwt),
):

    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    conversation = await get_conversation_by_id(payload.conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    history = conversation.get("history") or []
    reply = await generate_follow_up_message(conversation, payload.stage)

    return {
        "conversation_id": payload.conversation_id,
        "stage": payload.stage,
        "message": reply,
        "history_count": len(history),
    }


@app.post("/follow-ups/manychat-auto-23h")
async def manychat_auto_23h_follow_up(
    payload: ManyChatFollowUpPayload,
    user_id: str = Depends(require_jwt),
):

    subscriber_id = payload.subscriber_id.strip()
    if not subscriber_id:
        return {"ok": False, "message": "", "reason": "subscriber_id is required"}

    conversation = await get_contact(subscriber_id, user_id)
    if not conversation:
        return {"ok": False, "message": "", "reason": "Conversation not found"}

    due_item = build_follow_up_item(conversation)
    if not due_item or due_item.get("stage") != "auto_23h":
        return {"ok": False, "message": "", "reason": "Auto 23h follow-up is not due"}

    message = await generate_follow_up_message(conversation, "auto_23h")
    history = conversation.get("history") or []
    new_history = history + [{
        "role": "assistant",
        "content": message,
        "timestamp": now_iso(),
        "follow_up_stage": "auto_23h",
        "follow_up_mode": "manychat",
        "source": "follow_up_manychat",
    }]
    if len(new_history) > MAX_HISTORY_TURNS * 2:
        new_history = new_history[-(MAX_HISTORY_TURNS * 2):]

    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"username": f"eq.{subscriber_id}", "user_id": f"eq.{user_id}"},
            json={
                "response": message,
                "history": new_history,
                "status": "en_cours",
            },
            timeout=10.0,
        )
        res.raise_for_status()

    return {
        "ok": True,
        "message": message,
        "conversation_id": conversation.get("id"),
        "stage": "auto_23h",
        "reason": None,
    }


@app.post("/follow-ups/{conversation_id}/send-auto-23h")
async def send_auto_23h_follow_up(
    conversation_id: str,
    user_id: str = Depends(require_jwt),
):

    conversation = await get_conversation_by_id(conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    channel = conversation.get("channel") or "instagram"
    if channel == "instagram" and not MANYCHAT_API_KEY:
        raise HTTPException(status_code=500, detail="MANYCHAT_API_KEY is not configured")
    if channel == "whatsapp" and (not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID):
        raise HTTPException(status_code=500, detail="WhatsApp API is not configured")

    due_item = build_follow_up_item(conversation)
    if not due_item or due_item.get("stage") != "auto_23h":
        raise HTTPException(status_code=409, detail="Auto 23h follow-up is not due")

    message = await generate_follow_up_message(conversation, "auto_23h")
    send_result = await send_channel_message(conversation, message)
    if send_result["status_code"] >= 400:
        raise HTTPException(status_code=502, detail=f"Send error: {send_result['body']}")

    history = conversation.get("history") or []
    new_history = history + [{
        "role": "assistant",
        "content": message,
        "timestamp": now_iso(),
        "follow_up_stage": "auto_23h",
        "follow_up_mode": "auto",
        "source": "follow_up_auto",
    }]
    if len(new_history) > MAX_HISTORY_TURNS * 2:
        new_history = new_history[-(MAX_HISTORY_TURNS * 2):]

    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
            json={
                "response": message,
                "history": new_history,
                "status": "en_cours",
            },
            timeout=10.0,
        )
        res.raise_for_status()

    return {
        "conversation_id": conversation_id,
        "stage": "auto_23h",
        "message": message,
        "sent": True,
    }


# ── Playground endpoint ───────────────────────────────────────────────────────

@app.post("/playground")
async def playground(
    payload: PlaygroundPayload,
    user_id: str = Depends(require_jwt),
):

    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")
    system_prompt = build_angellos_beta_prompt(await get_active_prompt(user_id))
    if payload.calendly_url or payload.sales_page_url:
        system_prompt = append_agent_options(
            system_prompt,
            calendly_url=(payload.calendly_url or "").strip(),
            sales_page_url=(payload.sales_page_url or "").strip(),
        )
    try:
        reply = generate_claude_reply(payload.messages, system_prompt)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {e}")
    last_prospect_message = next(
        ((msg.get("content") or "") for msg in reversed(payload.messages) if msg.get("role") == "user"),
        "",
    )
    reply = sanitize_angellos_beta_reply(reply, last_prospect_message)
    return {"response": reply}


# ── Feedback Loop endpoints ────────────────────────────────────────────────────

@app.post("/feedback-loop")
async def run_feedback_loop(
    payload: FeedbackLoopPayload = FeedbackLoopPayload(),
    user_id: str = Depends(require_jwt),
):


    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    n = min(max(payload.n, 1), 50)

    # 1. Fetch engaged conversations (status != nouveau)
    try:
        async with httpx.AsyncClient() as http:
            res = await http.get(
                SUPABASE_CONVERSATIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={
                    "status": "neq.nouveau",
                    "order": "created_at.desc",
                    "limit": str(n * 3),  # margin for client-side filtering
                    "user_id": f"eq.{user_id}",
                },
                timeout=15.0,
            )
            res.raise_for_status()
            all_convs = res.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase error: {e}")

    # Filter: at least 2 messages in history
    convs = [
        c for c in all_convs
        if len(c.get("history") or []) >= 2
    ][:n]

    if not convs:
        raise HTTPException(status_code=422, detail="Not enough engaged conversations for analysis.")

    # 2. Fetch the active prompt
    system_prompt = await get_active_prompt(user_id)

    # 3. Format conversations and call Claude
    user_message = format_conversations_for_analysis(convs, system_prompt)

    if payload.manual_observations:
        user_message += f"\n\n=== MANUAL OBSERVATIONS ===\n{payload.manual_observations}"
    if payload.test_conversation:
        user_message += f"\n\n=== TEST CONVERSATION ===\n{payload.test_conversation}"

    try:
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=4096,
            system=build_analysis_prompt(config),
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {e}")

    # 4. Parse the JSON response
    try:
        # Clean possible markdown blocks ```json ... ```
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        analysis = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Invalid JSON from Claude: {e}. Raw: {raw[:200]}")

    # 5. Save into insights
    date_range_start = convs[-1].get("created_at") if convs else None
    date_range_end = convs[0].get("created_at") if convs else None

    insight_data = {
        **row_owner_fields(user_id),
        "conversations_analyzed": len(convs),
        "date_range_start": date_range_start,
        "date_range_end": date_range_end,
        "pain_points": analysis.get("pain_points", []),
        "objections": analysis.get("objections", []),
        "converting_profiles": analysis.get("converting_profiles", []),
        "drop_off_stages": analysis.get("drop_off_stages", []),
        "business_suggestions": analysis.get("business_suggestions", []),
        "prompt_current": system_prompt,
        "prompt_proposed": analysis.get("prompt_proposed", ""),
        "prompt_diff": analysis.get("prompt_diff", []),
        "status": "pending",
    }

    try:
        async with httpx.AsyncClient() as http:
            res = await http.post(
                SUPABASE_INSIGHTS_URL,
                headers={**supabase_headers(), "Prefer": "return=representation"},
                json=insight_data,
                timeout=10.0,
            )
            res.raise_for_status()
            created = res.json()
            insight = created[0] if isinstance(created, list) else created
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase insert error: {e}")

    print(f"[feedback-loop] insight created id={insight.get('id')} convs={len(convs)}")
    return insight


@app.post("/preview-prompt")
async def preview_prompt(
    payload: PreviewPromptPayload,
    user_id: str = Depends(require_jwt),
):
    """Generate a preview diff WITHOUT modifying the database."""


    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    prompt_actif = await get_active_prompt(user_id)

    def fmt(items: list[str], label: str) -> str:
        if not items:
            return f"{label}: (none)"
        return f"{label}:\n" + "\n".join(f"- {x}" for x in items)

    user_message = (
        f"Here is the active prompt of an Instagram setter agent:\n"
        f"<active_prompt>\n{prompt_actif}\n</active_prompt>\n"
        f"The user selected these elements to integrate:\n"
        f"{fmt(payload.selected_suggestions, 'Selected business suggestions')}\n"
        f"{fmt(payload.selected_pain_points, 'Detected pain points to integrate into qualification')}\n"
        f"{fmt(payload.selected_objections, 'Objections to handle better in the prompt')}\n"
        f'Generate the complete modified prompt, then return ONLY valid JSON:\n'
        f'{{"prompt_proposed": "<complete modified prompt>", "diff": [{{"line": "<text>", "type": "add|remove|keep", "justification": "<why>"}}]}}\n'
        f'In the diff: "add" = added or modified line, "remove" = deleted or replaced line, '
        f'"keep" = unchanged line near a change (context). '
        f'Justification is required only for each add/remove.\n'
        f"Return only JSON, with no text before or after."
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system="You are an expert in AI prompt optimization.",
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {e}")

    try:
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"Invalid JSON from Claude: {e}. Raw: {raw[:200]}")

    print(f"[preview-prompt] insight_id={payload.insight_id} diff_lines={len(result.get('diff', []))}")
    return {"prompt_proposed": result.get("prompt_proposed", ""), "diff": result.get("diff", [])}


@app.post("/apply-prompt")
async def apply_prompt(
    payload: ApplyPromptPayload,
    user_id: str = Depends(require_jwt),
):
    """Apply a prompt already built by /preview-prompt."""

    await require_owned_insight(payload.insight_id, user_id)

    # 1. Deactivate all active prompts
    try:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"is_active": "eq.true", **owner_scope(user_id)},
                json={"is_active": False},
                timeout=10.0,
            )
            res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase deactivate error: {e}")

    # 2. Insert the new active prompt
    try:
        async with httpx.AsyncClient() as http:
            res = await http.post(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=representation"},
                json={
                    **row_owner_fields(user_id),
                    "content": payload.prompt_proposed,
                    "is_active": True,
                    "source": "feedback-loop",
                    "insight_id": payload.insight_id,
                },
                timeout=10.0,
            )
            res.raise_for_status()
            created = res.json()
            new_version = created[0] if isinstance(created, list) else created
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase insert error: {e}")

    # 3. Mark the insight as applied
    try:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_INSIGHTS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{payload.insight_id}", **owner_scope(user_id)},
                json={"status": "applied"},
                timeout=10.0,
            )
            res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase update error: {e}")

    print(f"[apply-prompt] version_id={new_version.get('id')} insight_id={payload.insight_id}")
    return {"success": True, "prompt_version_id": new_version.get("id")}


@app.post("/refine-prompt")
async def refine_prompt(
    payload: RefinePromptPayload,
    user_id: str = Depends(require_jwt),
):
    """Surgical refinement of the active prompt from the Training Center."""

    instruction = payload.instruction.strip()
    if not instruction:
        raise HTTPException(status_code=422, detail="Instruction is required")

    active_version = await get_active_prompt_version(user_id)
    current_prompt = active_version.get("content") or build_system_prompt(config)
    if payload.active_prompt and not payload.apply:
        current_prompt = payload.active_prompt.strip()

    if payload.apply and payload.prompt_proposed:
        try:
            result = normalize_prompt_refinement_result(
                {
                    "updated_prompt": payload.prompt_proposed,
                    "target_section": "User validation",
                    "summary": "Previewed version validated from the Training Center.",
                    "changes": [],
                },
                current_prompt,
            )
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Invalid prompt_proposed: {e}")
    else:
        if client is None:
            raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")
        system = (
            "You are a senior prompt engineering expert for an Instagram setter agent. "
            "You must modify an existing prompt surgically. "
            "Never rewrite the whole prompt for a minor instruction. "
            "Keep the structure, technical tags, and business data intact unless the modification targets that section. "
            "Return only valid JSON, with no markdown."
        )
        user_message = (
            "User instruction:\n"
            f"{instruction}\n\n"
            "Active prompt:\n"
            f"<active_prompt>\n{current_prompt}\n</active_prompt>\n\n"
            "Analyze which section is concerned: tone, rules, qualification questions, follow-ups, price, objections, links, or guardrails.\n"
            "Apply only the minimum necessary modification. Expected examples:\n"
            '- "He uses too many emojis" => add/reinforce a zero-emoji rule in the tone section.\n'
            '- "He asks two questions in the same message" => reinforce one question per message.\n'
            '- "He answers about price too quickly" => add a qualification condition before mentioning terms.\n'
            "Return exactly this JSON:\n"
            "{\n"
            '  "updated_prompt": "<complete prompt after minimal modification>",\n'
            '  "target_section": "<identified section>",\n'
            '  "summary": "<short summary of the modification>",\n'
            '  "changes": ["<change 1>", "<change 2>"]\n'
            "}\n"
        )

        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=8192,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            raw = response.content[0].text.strip()
            result = normalize_prompt_refinement_result(parse_llm_json(raw), current_prompt)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Prompt refinement error: {e}")

    updated_prompt = result["updated_prompt"]
    visual_diff = build_prompt_diff(current_prompt, updated_prompt)
    response_payload = {
        "success": True,
        "applied": False,
        "prompt_proposed": updated_prompt,
        "updated_prompt": updated_prompt,
        "diff": visual_diff,
        "target_section": result["target_section"],
        "summary": result["summary"],
        "changes": result["changes"],
        "instruction": instruction,
        "reset_test_conversation": False,
    }

    if not payload.apply:
        print(f"[refine-prompt:preview] instruction_len={len(instruction)} diff_lines={len(visual_diff)}")
        return response_payload

    applied_at = now_iso()
    try:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"is_active": "eq.true", **owner_scope(user_id)},
                json={"is_active": False},
                timeout=10.0,
            )
            res.raise_for_status()

            insert_payload = {
                **row_owner_fields(user_id),
                "content": updated_prompt,
                "is_active": True,
                "source": prompt_refinement_source(instruction),
                "insight_id": None,
                "refinement_instruction": instruction,
                "refinement_applied_at": applied_at,
                "previous_version_id": active_version.get("id"),
                "prompt_diff": visual_diff,
            }
            res = await http.post(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=representation"},
                json=insert_payload,
                timeout=10.0,
            )
            res.raise_for_status()
            created = res.json()
            new_version = created[0] if isinstance(created, list) else created
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase prompt refinement save error: {e}")

    response_payload.update({
        "applied": True,
        "prompt_version_id": new_version.get("id"),
        "previous_version_id": active_version.get("id"),
        "refinement_applied_at": applied_at,
        "reset_test_conversation": True,
    })
    print(f"[refine-prompt:apply] version_id={new_version.get('id')} instruction_len={len(instruction)}")
    return response_payload


@app.get("/prompt-versions")
async def get_prompt_versions(
    user_id: str = Depends(require_jwt),
):

    try:
        async with httpx.AsyncClient() as http:
            res = await http.get(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={
                    "order": "created_at.desc",
                    "select": "id,created_at,is_active,source,insight_id,refinement_instruction,refinement_applied_at,previous_version_id",
                    **owner_scope(user_id),
                },
                timeout=10.0,
            )
            res.raise_for_status()
            return res.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase error: {e}")


@app.post("/prompt-versions/{version_id}/restore")
async def restore_prompt_version(
    version_id: str,
    user_id: str = Depends(require_jwt),
):

    await require_owned_prompt_version(version_id, user_id)

    try:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"is_active": "eq.true", **owner_scope(user_id)},
                json={"is_active": False},
                timeout=10.0,
            )
            res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase deactivate error: {e}")
    try:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{version_id}", **owner_scope(user_id)},
                json={"is_active": True},
                timeout=10.0,
            )
            res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase restore error: {e}")
    print(f"[restore-prompt] version_id={version_id}")
    return {"success": True}


@app.get("/agent-links")
async def get_agent_links(
    user_id: str = Depends(require_jwt),
):

    prompt = await get_active_prompt(user_id)
    return extract_agent_links(prompt)


@app.patch("/agent-links")
async def update_agent_links(
    payload: AgentLinksPayload,
    user_id: str = Depends(require_jwt),
):

    prompt = await get_active_prompt(user_id)
    next_prompt = append_agent_options(
        prompt,
        calendly_url=payload.calendly_url.strip(),
        sales_page_url=payload.sales_page_url.strip(),
    )

    try:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"is_active": "eq.true", **owner_scope(user_id)},
                json={"is_active": False},
                timeout=10.0,
            )
            res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase deactivate error: {e}")

    try:
        async with httpx.AsyncClient() as http:
            res = await http.post(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=representation"},
                json={
                    **row_owner_fields(user_id),
                    "content": next_prompt,
                    "is_active": True,
                    "source": "agent-options",
                    "insight_id": None,
                },
                timeout=10.0,
            )
            res.raise_for_status()
            created = res.json()
            new_version = created[0] if isinstance(created, list) else created
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase insert error: {e}")

    print(f"[agent-links] version_id={new_version.get('id')}")
    return {"success": True, "prompt_version_id": new_version.get("id")}


@app.get("/agent-profile")
async def get_agent_profile(
    user_id: str = Depends(require_jwt),
):
    prompt = await get_active_prompt(user_id)
    return extract_agent_profile(prompt)


@app.patch("/agent-profile")
async def update_agent_profile(
    payload: AgentProfilePayload,
    user_id: str = Depends(require_jwt),
):
    prompt = await get_active_prompt(user_id)
    next_prompt = append_agent_profile(prompt, payload.model_dump())

    try:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"is_active": "eq.true", **owner_scope(user_id)},
                json={"is_active": False},
                timeout=10.0,
            )
            res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase deactivate error: {e}")

    try:
        async with httpx.AsyncClient() as http:
            res = await http.post(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=representation"},
                json={
                    **row_owner_fields(user_id),
                    "content": next_prompt,
                    "is_active": True,
                    "source": "agent-profile",
                    "insight_id": None,
                },
                timeout=10.0,
            )
            res.raise_for_status()
            created = res.json()
            new_version = created[0] if isinstance(created, list) else created
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase insert error: {e}")

    print(f"[agent-profile] version_id={new_version.get('id')}")
    return {"success": True, "prompt_version_id": new_version.get("id")}


# ── Training Center endpoints ─────────────────────────────────────────────────

@app.get("/agent/training-center")
async def get_training_center(
    user_id: str = Depends(require_jwt),
):
    try:
        profile = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id)
        avatar = await get_user_singleton_row(SUPABASE_AGENT_AVATARS_URL, user_id)
        sales_rules = await get_user_singleton_row(SUPABASE_AGENT_SALES_RULES_URL, user_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase error: {e}")

    checklist = {
        "business_setup": bool(profile and profile.get("profile")),
        "avatar_client": bool(avatar and avatar.get("avatar")),
        "regles_dm": bool(sales_rules and sales_rules.get("rules")),
        "test_conversation": False,
    }
    completed = sum(1 for value in checklist.values() if value)
    return {
        "profile": profile,
        "avatar": avatar,
        "sales_rules": sales_rules,
        "checklist": checklist,
        "progress_score": round((completed / len(checklist)) * 100),
    }


@app.post("/agent/profile/save")
async def save_training_profile(
    payload: TrainingProfilePayload,
    user_id: str = Depends(require_jwt),
):
    profile = clean_json_value(payload.model_dump())
    try:
        row = await upsert_user_singleton_row(
            SUPABASE_AGENT_PROFILES_URL,
            user_id,
            {"profile": profile},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase upsert error: {e}")
    return {"success": True, "profile": row}


@app.patch("/agent/profile")
async def autosave_training_profile(
    payload: TrainingProfilePayload,
    user_id: str = Depends(require_jwt),
):
    profile = clean_json_value(payload.model_dump())
    try:
        row = await upsert_user_singleton_row(
            SUPABASE_AGENT_PROFILES_URL,
            user_id,
            {"profile": profile},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase upsert error: {e}")
    return {"success": True, "profile": row}


@app.post("/agent/avatar/generate")
async def generate_agent_avatar(
    payload: AvatarGeneratePayload,
    user_id: str = Depends(require_jwt),
):
    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    system = (
        "You are a CRM and sales enablement strategist for an Instagram setter. "
        "Transform raw answers into a client avatar usable by an AI agent. "
        "Return only valid JSON, with no markdown."
    )
    user_message = (
        "User answers:\n"
        f"{json.dumps(payload.model_dump(), ensure_ascii=False, indent=2)}\n\n"
        "Generate exactly this JSON structure:\n"
        "{\n"
        '  "persona_summary": "",\n'
        '  "current_situation": "",\n'
        '  "desired_situation": "",\n'
        '  "pain_points": [],\n'
        '  "fears": [],\n'
        '  "frustrations": [],\n'
        '  "objections": [],\n'
        '  "buying_triggers": [],\n'
        '  "dream_outcomes": [],\n'
        '  "exact_words": [],\n'
        '  "bad_fit": [],\n'
        '  "confidence_score": 0\n'
        "}\n"
        "confidence_score is an integer from 0 to 100 based on input precision."
    )
    try:
        raw = generate_claude_reply([{"role": "user", "content": user_message}], system)
        avatar = clean_json_value(parse_llm_json(raw))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Avatar generation error: {e}")

    return {"avatar": avatar}


@app.post("/agent/avatar/save")
async def save_agent_avatar(
    payload: AvatarSavePayload,
    user_id: str = Depends(require_jwt),
):
    try:
        row = await upsert_user_singleton_row(
            SUPABASE_AGENT_AVATARS_URL,
            user_id,
            {
                "source_inputs": clean_json_value(payload.source_inputs.model_dump()),
                "avatar": clean_json_value(payload.avatar),
            },
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase upsert error: {e}")
    return {"success": True, "avatar": row}


@app.patch("/agent/avatar/source-inputs")
async def autosave_agent_avatar_source_inputs(
    payload: AvatarGeneratePayload,
    user_id: str = Depends(require_jwt),
):
    try:
        row = await upsert_user_singleton_row(
            SUPABASE_AGENT_AVATARS_URL,
            user_id,
            {"source_inputs": clean_json_value(payload.model_dump())},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase upsert error: {e}")
    return {"success": True, "avatar": row}


@app.patch("/agent/avatar")
async def autosave_agent_avatar(
    payload: AvatarDataPayload,
    user_id: str = Depends(require_jwt),
):
    try:
        row = await upsert_user_singleton_row(
            SUPABASE_AGENT_AVATARS_URL,
            user_id,
            {"avatar": clean_json_value(payload.avatar)},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase upsert error: {e}")
    return {"success": True, "avatar": row}


@app.post("/agent/sales-rules/generate")
async def generate_agent_sales_rules(
    payload: SalesRulesGeneratePayload = SalesRulesGeneratePayload(),
    user_id: str = Depends(require_jwt),
):
    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    try:
        profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id)
        avatar_row = await get_user_singleton_row(SUPABASE_AGENT_AVATARS_URL, user_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase error: {e}")

    profile = payload.profile or (profile_row or {}).get("profile") or {}
    avatar = payload.avatar or (avatar_row or {}).get("avatar") or {}
    if not profile or not avatar:
        raise HTTPException(status_code=422, detail="Business profile and avatar are required")

    system = (
        "You are an expert in Instagram DM qualification for coaches and infopreneurs. "
        "Create simple, operational, and non-aggressive rules for an AI setter agent. "
        "Return only valid JSON, with no markdown."
    )
    user_message = (
        "Business profile:\n"
        f"{json.dumps(profile, ensure_ascii=False, indent=2)}\n\n"
        "Client avatar:\n"
        f"{json.dumps(avatar, ensure_ascii=False, indent=2)}\n\n"
        "Generate exactly this JSON structure:\n"
        "{\n"
        '  "qualification_questions": [],\n'
        '  "buying_signals": [],\n'
        '  "call_offer_conditions": [],\n'
        '  "red_flags": [],\n'
        '  "stop_conditions": [],\n'
        '  "objection_responses": [],\n'
        '  "follow_up_rules": [],\n'
        '  "do_not_say": [],\n'
        '  "escalation_rules": []\n'
        "}\n"
        "Each list must contain short, concrete sentences."
    )
    try:
        raw = generate_claude_reply([{"role": "user", "content": user_message}], system)
        rules = clean_json_value(parse_llm_json(raw))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Sales rules generation error: {e}")

    return {"rules": rules}


@app.post("/agent/sales-rules/save")
async def save_agent_sales_rules(
    payload: SalesRulesSavePayload,
    user_id: str = Depends(require_jwt),
):
    try:
        row = await upsert_user_singleton_row(
            SUPABASE_AGENT_SALES_RULES_URL,
            user_id,
            {"rules": clean_json_value(payload.rules)},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase upsert error: {e}")
    return {"success": True, "sales_rules": row}


@app.patch("/agent/sales-rules")
async def autosave_agent_sales_rules(
    payload: SalesRulesSavePayload,
    user_id: str = Depends(require_jwt),
):
    try:
        row = await upsert_user_singleton_row(
            SUPABASE_AGENT_SALES_RULES_URL,
            user_id,
            {"rules": clean_json_value(payload.rules)},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase upsert error: {e}")
    return {"success": True, "sales_rules": row}


@app.post("/agent/prompt/rebuild")
async def rebuild_agent_prompt_from_training_center(
    user_id: str = Depends(require_jwt),
):
    try:
        profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id)
        avatar_row = await get_user_singleton_row(SUPABASE_AGENT_AVATARS_URL, user_id)
        sales_rules_row = await get_user_singleton_row(SUPABASE_AGENT_SALES_RULES_URL, user_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase error: {e}")

    profile = (profile_row or {}).get("profile") or {}
    avatar = (avatar_row or {}).get("avatar") or {}
    sales_rules = (sales_rules_row or {}).get("rules") or {}
    if not profile:
        raise HTTPException(status_code=422, detail="Business profile is required")

    active_prompt = await get_active_prompt(user_id)
    next_prompt = build_training_center_prompt(active_prompt, profile, avatar, sales_rules)

    try:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"is_active": "eq.true", **owner_scope(user_id)},
                json={"is_active": False},
                timeout=10.0,
            )
            res.raise_for_status()

            res = await http.post(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Prefer": "return=representation"},
                json={
                    **row_owner_fields(user_id),
                    "content": next_prompt,
                    "is_active": True,
                    "source": "training-center",
                    "insight_id": None,
                },
                timeout=10.0,
            )
            res.raise_for_status()
            created = res.json()
            new_version = created[0] if isinstance(created, list) else created
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase prompt rebuild error: {e}")

    print(f"[training-center] prompt_version_id={new_version.get('id')}")
    return {"success": True, "prompt_version_id": new_version.get("id")}


@app.get("/insights")
async def get_insights(
    user_id: str = Depends(require_jwt),
):


    try:
        async with httpx.AsyncClient() as http:
            res = await http.get(
                SUPABASE_INSIGHTS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={
                    "order": "created_at.desc",
                    "select": "id,created_at,conversations_analyzed,status,pain_points,objections,business_suggestions,prompt_diff,prompt_proposed",
                    **owner_scope(user_id),
                },
                timeout=10.0,
            )
            res.raise_for_status()
            return res.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase error: {e}")


@app.patch("/insights/{insight_id}/ignore")
async def ignore_insight(
    insight_id: str,
    user_id: str = Depends(require_jwt),
):


    try:
        async with httpx.AsyncClient() as http:
            res = await http.patch(
                SUPABASE_INSIGHTS_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                params={"id": f"eq.{insight_id}", **owner_scope(user_id)},
                json={"status": "ignored"},
                timeout=10.0,
            )
            res.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase error: {e}")

    return {"success": True}
