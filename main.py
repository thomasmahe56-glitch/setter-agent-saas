from fastapi import FastAPI, Header, HTTPException, Depends, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from anthropic import Anthropic
from dotenv import load_dotenv
from config import load_config
from prompts import build_system_prompt, build_analysis_prompt, build_follow_up_prompt, build_conversation_review_prompt
import hmac
import httpx
import hashlib
import difflib
import base64
import io
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
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
SUPABASE_CONVERSATION_REVIEWS_URL = f"{config.supabase_url}/conversation_reviews"
SUPABASE_AGENT_PROFILES_URL = f"{config.supabase_url}/agent_profiles"
SUPABASE_AGENT_AVATARS_URL = f"{config.supabase_url}/agent_avatars"
SUPABASE_AGENT_SALES_RULES_URL = f"{config.supabase_url}/agent_sales_rules"
SUPABASE_BETA_AI_USAGE_URL = f"{config.supabase_url}/beta_ai_usage"
SUPABASE_BETA_ACCOUNT_SETTINGS_URL = f"{config.supabase_url}/beta_account_settings"
MANYCHAT_API_KEY = config.manychat_token
MANYCHAT_SEND_URL = "https://api.manychat.com/fb/sending/sendContent"
WHATSAPP_ACCESS_TOKEN = config.whatsapp_access_token
WHATSAPP_PHONE_NUMBER_ID = config.whatsapp_phone_number_id
WHATSAPP_VERIFY_TOKEN = config.whatsapp_verify_token
META_APP_SECRET = config.meta_app_secret
GRAPH_API_VERSION = config.graph_api_version or "v23.0"
WHATSAPP_SEND_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
MAX_HISTORY_TURNS = 40
DEFAULT_BETA_COST_CAP_EUR = float(os.environ.get("DEFAULT_BETA_COST_CAP_EUR", "50"))
CLAUDE_SONNET_4_6_INPUT_EUR_PER_MTOKEN = float(os.environ.get("CLAUDE_SONNET_4_6_INPUT_EUR_PER_MTOKEN", "2.75"))
CLAUDE_SONNET_4_6_OUTPUT_EUR_PER_MTOKEN = float(os.environ.get("CLAUDE_SONNET_4_6_OUTPUT_EUR_PER_MTOKEN", "13.75"))
AI_COST_BLOCK_USER_MESSAGE = "Angellos a atteint le plafond de coût IA configuré pour ce compte. Les nouvelles réponses automatiques sont arrêtées par sécurité."

GENERIC_AI_USER_MESSAGE = "Angellos couldn’t generate a reply right now. Please check your AI credits or try again."
LOW_CREDITS_USER_MESSAGE = "Angellos couldn’t generate a reply because the Anthropic account has no available credits. Add credits in Anthropic billing, then try again."
PROMPT_REFINEMENT_SAVE_USER_MESSAGE = "Angellos couldn’t save this update. Please try again."
PROMPT_REFINEMENT_SAVE_HINT = "If the issue continues, check the Training Center database configuration."


class ProviderGenerationError(Exception):
    def __init__(self, error_type: str, message: str, user_message: str, status_code: int = 502):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.user_message = user_message
        self.status_code = status_code


class CostCapExceededError(Exception):
    def __init__(self, user_id: str, spent_eur: float, cap_eur: float):
        super().__init__(f"AI cost cap reached for user {user_id}: {spent_eur:.4f}/{cap_eur:.2f} EUR")
        self.user_id = user_id
        self.spent_eur = spent_eur
        self.cap_eur = cap_eur
        self.user_message = AI_COST_BLOCK_USER_MESSAGE


def classify_provider_error(error: Exception) -> ProviderGenerationError:
    text = str(error)
    lowered = text.lower()
    print(f"[provider:classify] {type(error).__name__}: {text[:500]}", flush=True)
    if any(marker in lowered for marker in ["credit balance", "credits", "billing", "insufficient_credit"]):
        return ProviderGenerationError(
            "provider_billing",
            f"Anthropic credits exhausted: {text[:300]}",
            LOW_CREDITS_USER_MESSAGE,
            status_code=402,
        )
    if "rate" in lowered or "429" in lowered:
        return ProviderGenerationError(
            "provider_rate_limit",
            f"Anthropic rate limit: {text[:300]}",
            GENERIC_AI_USER_MESSAGE,
            status_code=429,
        )
    if "timeout" in lowered or "timed out" in lowered:
        return ProviderGenerationError(
            "provider_timeout",
            f"Anthropic timeout: {text[:300]}",
            GENERIC_AI_USER_MESSAGE,
            status_code=504,
        )
    if "not_found" in lowered or "404" in text:
        return ProviderGenerationError(
            "provider_model_not_found",
            f"Anthropic model not found (possibly deprecated): {text[:300]}",
            GENERIC_AI_USER_MESSAGE,
            status_code=502,
        )
    if "invalid request" in lowered or "400" in lowered:
        return ProviderGenerationError(
            "provider_invalid_request",
            f"Anthropic rejected request: {text[:300]}",
            GENERIC_AI_USER_MESSAGE,
            status_code=502,
        )
    return ProviderGenerationError(
        "provider_error",
        f"Anthropic error ({type(error).__name__}): {text[:300]}",
        GENERIC_AI_USER_MESSAGE,
        status_code=502,
    )


def provider_error_payload(error: ProviderGenerationError) -> dict:
    return {
        "ok": False,
        "error_type": error.error_type,
        "message": error.message,
        "user_message": error.user_message,
    }


def provider_error_response(error: ProviderGenerationError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=provider_error_payload(error))


def prompt_refinement_save_error_response(error: Exception) -> JSONResponse:
    status_code = 502
    if isinstance(error, httpx.HTTPStatusError):
        status_code = 502
        response = error.response
        print(
            "[refine-prompt:apply:error] "
            f"status={response.status_code} url={response.request.url} body={response.text[:1000]}"
        )
    else:
        print(f"[refine-prompt:apply:error] {type(error).__name__}: {error}")
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "ok": False,
            "error_type": "prompt_refinement_save_failed",
            "message": PROMPT_REFINEMENT_SAVE_USER_MESSAGE,
            "user_message": PROMPT_REFINEMENT_SAVE_USER_MESSAGE,
            "hint": PROMPT_REFINEMENT_SAVE_HINT,
        },
    )


def is_supabase_schema_cache_error(error: Exception) -> bool:
    if not isinstance(error, httpx.HTTPStatusError):
        return False
    if error.response.status_code != 400:
        return False
    body = error.response.text.lower()
    return (
        "schema cache" in body
        or "could not find" in body
        or "column" in body
        or "pgrst204" in body
    )
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
MANUAL_FOLLOW_UP_3_HOURS = 720  # j+30 (30 jours)
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
- New beta outreach conversations run in auto mode by default because Thomas explicitly approved automatic replies on 2026-07-03."""

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


def estimate_token_count(text: str) -> int:
    # Deterministic approximation when provider billing usage is not persisted: ~4 chars/token.
    return max(1, (len(text or "") + 3) // 4)


def estimate_claude_cost_eur(input_text: str, output_text: str) -> float:
    input_tokens = estimate_token_count(input_text)
    output_tokens = estimate_token_count(output_text)
    return (input_tokens / 1_000_000 * CLAUDE_SONNET_4_6_INPUT_EUR_PER_MTOKEN) + (
        output_tokens / 1_000_000 * CLAUDE_SONNET_4_6_OUTPUT_EUR_PER_MTOKEN
    )


async def get_beta_cost_settings(user_id: str) -> dict:
    settings = {"cap_eur": DEFAULT_BETA_COST_CAP_EUR, "enabled": True}
    try:
        async with httpx.AsyncClient() as http:
            res = await http.get(
                SUPABASE_BETA_ACCOUNT_SETTINGS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={"user_id": f"eq.{user_id}", "select": "ai_cost_cap_eur,ai_cost_guardrail_enabled", "limit": "1"},
                timeout=5.0,
            )
            if res.status_code == 404:
                try:
                    profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id, select="profile")
                    profile = (profile_row or {}).get("profile") or {}
                    if profile.get("beta_ai_cost_cap_eur") is not None:
                        settings["cap_eur"] = float(profile["beta_ai_cost_cap_eur"])
                    if profile.get("beta_ai_cost_guardrail_enabled") is not None:
                        settings["enabled"] = bool(profile["beta_ai_cost_guardrail_enabled"])
                except Exception as profile_error:
                    print(f"[ai-cost:settings] profile fallback unavailable error={type(profile_error).__name__}: {profile_error}", flush=True)
                return settings
            if res.status_code >= 400:
                print(f"[ai-cost:settings] fallback status={res.status_code} body={res.text[:200]}", flush=True)
                try:
                    profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id, select="profile")
                    profile = (profile_row or {}).get("profile") or {}
                    if profile.get("beta_ai_cost_cap_eur") is not None:
                        settings["cap_eur"] = float(profile["beta_ai_cost_cap_eur"])
                    if profile.get("beta_ai_cost_guardrail_enabled") is not None:
                        settings["enabled"] = bool(profile["beta_ai_cost_guardrail_enabled"])
                except Exception as profile_error:
                    print(f"[ai-cost:settings] profile fallback unavailable error={type(profile_error).__name__}: {profile_error}", flush=True)
                return settings
            rows = res.json()
            if rows:
                row = rows[0]
                if row.get("ai_cost_cap_eur") is not None:
                    settings["cap_eur"] = float(row["ai_cost_cap_eur"])
                if row.get("ai_cost_guardrail_enabled") is not None:
                    settings["enabled"] = bool(row["ai_cost_guardrail_enabled"])
    except Exception as e:
        print(f"[ai-cost:settings] fallback error={type(e).__name__}: {e}", flush=True)
    return settings


async def get_estimated_ai_spend_eur(user_id: str) -> float:
    try:
        async with httpx.AsyncClient() as http:
            res = await http.get(
                SUPABASE_BETA_AI_USAGE_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={"user_id": f"eq.{user_id}", "select": "estimated_cost_eur", "limit": "10000"},
                timeout=8.0,
            )
            if res.status_code >= 400:
                print(f"[ai-cost:usage] table unavailable status={res.status_code} body={res.text[:200]}", flush=True)
                try:
                    profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id, select="profile")
                    profile = (profile_row or {}).get("profile") or {}
                    return float(profile.get("beta_ai_estimated_spend_eur") or 0.0)
                except Exception as profile_error:
                    print(f"[ai-cost:usage] profile fallback unavailable error={type(profile_error).__name__}: {profile_error}", flush=True)
                    return 0.0
            return sum(float(row.get("estimated_cost_eur") or 0) for row in res.json())
    except Exception as e:
        print(f"[ai-cost:usage] fallback error={type(e).__name__}: {e}", flush=True)
        try:
            profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id, select="profile")
            profile = (profile_row or {}).get("profile") or {}
            return float(profile.get("beta_ai_estimated_spend_eur") or 0.0)
        except Exception as profile_error:
            print(f"[ai-cost:usage] profile fallback unavailable error={type(profile_error).__name__}: {profile_error}", flush=True)
            return 0.0


async def enforce_ai_cost_cap(user_id: str) -> dict:
    settings = await get_beta_cost_settings(user_id)
    spent = await get_estimated_ai_spend_eur(user_id)
    cap = float(settings["cap_eur"])
    if settings.get("enabled", True) and spent >= cap:
        raise CostCapExceededError(user_id, spent, cap)
    return {"spent_eur": spent, "cap_eur": cap, "enabled": settings.get("enabled", True)}


async def record_ai_usage_event(user_id: str, feature: str, input_text: str, output_text: str, model: str = "claude-sonnet-4-6") -> dict:
    input_tokens = estimate_token_count(input_text)
    output_tokens = estimate_token_count(output_text)
    cost = estimate_claude_cost_eur(input_text, output_text)
    row = {
        "user_id": user_id,
        "feature": feature,
        "model": model,
        "input_tokens_estimated": input_tokens,
        "output_tokens_estimated": output_tokens,
        "estimated_cost_eur": round(cost, 8),
    }
    try:
        async with httpx.AsyncClient() as http:
            res = await http.post(
                SUPABASE_BETA_AI_USAGE_URL,
                headers={**supabase_headers(), "Prefer": "return=minimal"},
                json=row,
                timeout=5.0,
            )
            if res.status_code >= 400:
                print(f"[ai-cost:record] table unavailable status={res.status_code} body={res.text[:200]}", flush=True)
                try:
                    profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id, select="profile")
                    profile = dict((profile_row or {}).get("profile") or {})
                    profile["beta_ai_estimated_spend_eur"] = round(float(profile.get("beta_ai_estimated_spend_eur") or 0.0) + cost, 8)
                    profile["beta_ai_last_usage_event"] = {**row, "created_at": now_iso()}
                    profile.setdefault("beta_ai_cost_cap_eur", DEFAULT_BETA_COST_CAP_EUR)
                    profile.setdefault("beta_ai_cost_guardrail_enabled", True)
                    await upsert_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id, {"profile": profile})
                except Exception as profile_error:
                    print(f"[ai-cost:record] profile fallback unavailable error={type(profile_error).__name__}: {profile_error}", flush=True)
    except Exception as e:
        print(f"[ai-cost:record] fallback error={type(e).__name__}: {e}", flush=True)
        try:
            profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id, select="profile")
            profile = dict((profile_row or {}).get("profile") or {})
            profile["beta_ai_estimated_spend_eur"] = round(float(profile.get("beta_ai_estimated_spend_eur") or 0.0) + cost, 8)
            profile["beta_ai_last_usage_event"] = {**row, "created_at": now_iso()}
            profile.setdefault("beta_ai_cost_cap_eur", DEFAULT_BETA_COST_CAP_EUR)
            profile.setdefault("beta_ai_cost_guardrail_enabled", True)
            await upsert_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id, {"profile": profile})
        except Exception as profile_error:
            print(f"[ai-cost:record] profile fallback unavailable error={type(profile_error).__name__}: {profile_error}", flush=True)
    return row


def cost_cap_error_payload(error: CostCapExceededError) -> dict:
    return {
        "ok": False,
        "error_type": "ai_cost_cap_reached",
        "message": error.user_message,
        "user_message": error.user_message,
        "spent_eur": round(error.spent_eur, 4),
        "cap_eur": round(error.cap_eur, 2),
    }


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


def extract_training_center_payload(prompt: str) -> dict:
    block_match = re.search(
        rf"{re.escape(TRAINING_CENTER_START)}(.*?){re.escape(TRAINING_CENTER_END)}",
        prompt or "",
        flags=re.DOTALL,
    )
    if not block_match:
        return {}
    try:
        payload = json.loads(block_match.group(1).strip())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def normalize_tenant_language(value: object) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"fr", "français", "francais", "french"}:
        return "fr"
    return "en"


def tenant_language_label(language: str) -> str:
    return "French" if normalize_tenant_language(language) == "fr" else "English"


def training_center_profile(prompt: str) -> dict:
    payload = extract_training_center_payload(prompt)
    profile = payload.get("agent_profile") if isinstance(payload, dict) else {}
    return profile if isinstance(profile, dict) else {}


def tenant_language_from_prompt(prompt: str) -> str:
    profile = training_center_profile(prompt)
    return normalize_tenant_language(
        profile.get("language")
        or profile.get("preferred_language")
        or profile.get("default_language")
    )


def is_angellos_acquisition_prompt(prompt: str) -> bool:
    profile = training_center_profile(prompt)
    use_case = str(
        profile.get("agent_use_case")
        or profile.get("mode")
        or profile.get("use_case")
        or ""
    ).strip().lower()
    if profile.get("is_angellos_acquisition") is True or use_case in {"acquisition", "angellos_acquisition", "angellos-beta", "angellos_beta"}:
        return True
    return False


def default_automation_mode_for_prompt(prompt: str) -> str:
    """Keep Thomas' acquisition flow auto, but tenant beta client accounts supervised by default."""
    return "auto" if is_angellos_acquisition_prompt(prompt) else "supervised"


def strip_angellos_beta_defaults(prompt: str) -> str:
    return re.sub(
        r"\n*ANGELLOS BETA DEFAULTS\n.*?(?=\nQUALIFICATION PROCESS\n)",
        "\n",
        prompt or "",
        flags=re.DOTALL,
    ).strip()


def tenant_reply_rules(language: str) -> str:
    language = normalize_tenant_language(language)
    default_language = tenant_language_label(language)
    french_line = "- Write natural, short French by default. Use another language only when the prospect clearly uses it first." if language == "fr" else "- Write natural, short English by default. Mirror another language only when the prospect clearly uses it first."
    return f"""=== TENANT CLIENT MODE — SOURCE OF TRUTH ===
Mode: tenant client setter
Default tenant language: {default_language}

These tenant rules override any older Angellos beta defaults, fallback prompts, canned examples, or generic acquisition copy.
- You are replying as this client's setter for this client's prospects.
- Use the Training Center data as the source of truth: niche, offer, price, qualification rules, tone, forbidden phrases, and next step.
- Never pitch Angellos, the Angellos beta, or a free 30-day beta unless the Training Center explicitly says the client's offer is Angellos.
- If the prospect asks price, answer from the client's Training Center price/terms only. If missing, say you need to check fit first or that the operator will confirm, but do not invent a price.
- If the prospect asks what it is or how it works, explain the client's offer/process, not Angellos.
- Move toward the client's configured next step: calendly_url, sales_page_url, or next_step.
{french_line}
- Keep replies conversational, concise, and human. Ask one question maximum.
- Do not use Angellos acquisition canned replies in tenant mode."""


def build_tenant_prompt(base_prompt: str) -> str:
    clean_prompt = strip_angellos_beta_defaults(base_prompt)
    return f"{clean_prompt}\n\n{tenant_reply_rules(tenant_language_from_prompt(base_prompt))}"


def build_generation_prompt(base_prompt: str) -> str:
    if is_angellos_acquisition_prompt(base_prompt):
        return build_angellos_beta_prompt(base_prompt)
    return build_tenant_prompt(base_prompt)


_PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
# ManyChat subscriber IDs are pure numeric strings (typically 7–10 digits).
# An Instagram handle may contain digits but is never purely numeric and long.
_NUMERIC_ID_RE = re.compile(r"^\d{6,}$")
_PLACEHOLDER_DISPLAY_NAMES = {
    "instagram prospect",
    "prospect instagram",
    "prospect",
    "unknown",
    "undefined",
    "null",
    "none",
    "n/a",
    "na",
}


def is_placeholder_display_name(value: Optional[str]) -> bool:
    """Return True if value is an unresolved placeholder or a raw numeric subscriber ID."""
    if not value:
        return False
    cleaned = (value or "").strip()
    normalized = re.sub(r"\s+", " ", cleaned).strip().lower()
    return (
        bool(_PLACEHOLDER_RE.search(cleaned))
        or bool(_NUMERIC_ID_RE.match(cleaned))
        or normalized in _PLACEHOLDER_DISPLAY_NAMES
    )


def is_real_instagram_username(value: Optional[str]) -> bool:
    """Return True for a usable Instagram handle/display token, never for placeholders or subscriber IDs."""
    cleaned = (value or "").strip().lstrip("@")
    if not cleaned or is_placeholder_display_name(cleaned):
        return False
    if len(cleaned) > 200:
        return False
    return bool(re.match(r"^[A-Za-z0-9._]+$", cleaned))


def unresolved_instagram_display_name(external_contact_id: Optional[str]) -> str:
    suffix = re.sub(r"\D", "", external_contact_id or "")[-4:] or "pending"
    return f"Unresolved Instagram contact {suffix}"


def normalize_display_name(
    incoming: Optional[str],
    existing: Optional[str] = None,
    external_contact_id: Optional[str] = None,
) -> str:
    """Return a safe display name, never an unresolved placeholder or bare subscriber ID.

    Priority:
    1. If incoming is valid (no placeholder, not a numeric ID), use it.
    2. If incoming is invalid but existing is valid, keep existing.
    3. Otherwise fall back to a unique non-final internal label.
    """
    incoming_clean = (incoming or "").strip()
    if incoming_clean and not is_placeholder_display_name(incoming_clean):
        return incoming_clean.lstrip("@")
    if incoming_clean and is_placeholder_display_name(incoming_clean):
        print(f"[display_name] rejected invalid display_name: {incoming_clean!r}")
    existing_clean = (existing or "").strip()
    if existing_clean and not is_placeholder_display_name(existing_clean):
        return existing_clean.lstrip("@")
    return unresolved_instagram_display_name(external_contact_id)


def walk_json_values(value: object):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from walk_json_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_json_values(item)


def extract_manychat_ig_username(payload: object) -> Optional[str]:
    """Extract a real IG username from known ManyChat fields or custom fields."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    priority_keys = (
        "ig_username",
        "instagram_username",
        "instagram_user_name",
        "instagram_handle",
        "ig_handle",
        "username",
        "user_name",
    )
    candidates: list[str] = []
    for key in priority_keys:
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, str):
            candidates.append(value)

    def collect_named_custom_fields(value: object) -> None:
        if isinstance(value, dict):
            field_name = str(value.get("name") or value.get("field_name") or value.get("key") or "")
            field_key = field_name.lower().replace(" ", "_")
            if any(marker in field_key for marker in ("ig_username", "instagram_username", "instagram_handle", "ig_handle", "username")):
                for nested_key in ("value", "text"):
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, str):
                        candidates.append(nested_value)
            for nested in value.values():
                collect_named_custom_fields(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_named_custom_fields(nested)

    collect_named_custom_fields(data)

    for key, value in walk_json_values(data):
        key_normalized = key.lower().replace(" ", "_")
        if any(marker in key_normalized for marker in ("ig_username", "instagram_username", "instagram_handle", "ig_handle", "username")):
            if isinstance(value, str):
                candidates.append(value)
            elif isinstance(value, dict):
                for nested_key in ("value", "text", "name"):
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, str):
                        candidates.append(nested_value)
        if isinstance(value, dict):
            field_name = str(value.get("name") or value.get("field_name") or value.get("key") or "")
            field_key = field_name.lower().replace(" ", "_")
            if any(marker in field_key for marker in ("ig_username", "instagram_username", "instagram_handle", "ig_handle", "username")):
                for nested_key in ("value", "text"):
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, str):
                        candidates.append(nested_value)

    for candidate in candidates:
        cleaned = candidate.strip().lstrip("@")
        if is_real_instagram_username(cleaned):
            return cleaned
    return None


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def clean_text_list(value: object, limit: int = 8) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    cleaned: list[str] = []
    for item in values:
        text = clean_text(item)
        if text:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def first_text(*values: object) -> str:
    for value in values:
        text = clean_text(value)
        if text:
            return text
    return ""


def sentence_join(items: list[str], empty: str) -> str:
    if not items:
        return empty
    return " ".join(item.rstrip(".") + "." for item in items if item)


def version_created_from(version: dict) -> dict:
    source = clean_text(version.get("source")) or "Manual update"
    instruction = clean_text(version.get("refinement_instruction"))
    if not instruction and source.startswith("training-refine:"):
        instruction = source.removeprefix("training-refine:").strip()
    if instruction:
        return {
            "label": "Correction",
            "detail": instruction[:220],
        }
    if source == "training-center":
        return {"label": "Knowledge & voice upload", "detail": ""}
    if source == "agent-profile":
        return {"label": "Offer update", "detail": ""}
    if source == "agent-options":
        return {"label": "Link settings", "detail": ""}
    if source == "feedback-loop":
        return {"label": "Conversation analysis", "detail": ""}
    return {"label": source, "detail": ""}


def prompt_version_memory_snapshot(version: dict, previous_version: Optional[dict] = None) -> dict:
    content = version.get("content") or ""
    payload = extract_training_center_payload(content)
    profile = payload.get("agent_profile") if isinstance(payload.get("agent_profile"), dict) else {}
    avatar = payload.get("agent_avatar") if isinstance(payload.get("agent_avatar"), dict) else {}
    rules = payload.get("agent_sales_rules") if isinstance(payload.get("agent_sales_rules"), dict) else {}

    legacy_profile = extract_agent_profile(content)
    if not profile and legacy_profile:
        profile = legacy_profile

    offer_items = [
        first_text(profile.get("business_name"), profile.get("offer_name")),
        first_text(profile.get("offer_promise")),
        first_text(profile.get("offer_format")),
        first_text(profile.get("price")),
    ]
    offer_summary = sentence_join([item for item in offer_items if item], "No offer details were saved in this version.")

    ideal_customer_items = [
        first_text(avatar.get("persona_summary"), profile.get("avatar_client")),
        *clean_text_list(avatar.get("pain_points"), 3),
        *clean_text_list(avatar.get("objections"), 2),
    ]
    ideal_customer = sentence_join(
        [item for item in ideal_customer_items if item],
        "No ideal customer summary was saved in this version.",
    )

    sales_process_raw = clean_text(profile.get("sales_process"))
    process_steps = clean_text_list(sales_process_raw.splitlines() if sales_process_raw else [], 8)
    if not process_steps:
        process_steps = [
            *clean_text_list(rules.get("qualification_questions"), 4),
            *clean_text_list(rules.get("call_offer_conditions"), 3),
        ]
    sales_process = [
        f"Step {index + 1}: {step.rstrip('.')}"
        for index, step in enumerate(process_steps[:8])
    ]

    next_step = first_text(profile.get("next_step"))
    if not next_step:
        next_step = sentence_join(
            clean_text_list(rules.get("call_offer_conditions"), 2),
            "No specific next step was saved in this version.",
        )

    voice_items = [
        first_text(profile.get("voice_profile")),
        *clean_text_list(profile.get("tone_rules"), 4),
    ]
    voice = sentence_join(
        [item for item in voice_items if item],
        "Direct, human, concise. Ask one question at a time.",
    )

    conversation_rules = [
        *clean_text_list(rules.get("qualification_questions"), 4),
        *clean_text_list(rules.get("buying_signals"), 3),
        *clean_text_list(rules.get("call_offer_conditions"), 3),
        *clean_text_list(rules.get("follow_up_rules"), 3),
        *clean_text_list(rules.get("escalation_rules"), 2),
    ][:12]

    forbidden_topics = [
        *clean_text_list(profile.get("forbidden_phrases"), 6),
        *clean_text_list(rules.get("do_not_say"), 6),
        *clean_text_list(rules.get("red_flags"), 3),
        *clean_text_list(rules.get("stop_conditions"), 3),
    ][:12]

    changes: list[str] = []
    prompt_diff = version.get("prompt_diff") if isinstance(version.get("prompt_diff"), list) else []
    added = [clean_text(item.get("line")) for item in prompt_diff if isinstance(item, dict) and item.get("type") == "add"]
    removed = [clean_text(item.get("line")) for item in prompt_diff if isinstance(item, dict) and item.get("type") == "remove"]
    if added or removed:
        if removed:
            changes.append(f"Before: {removed[0][:220]}")
        if added:
            changes.append(f"After: {added[0][:220]}")

    if previous_version and not changes:
        previous_snapshot = prompt_version_memory_snapshot(previous_version)
        previous_next_step = previous_snapshot.get("next_step") or ""
        if previous_next_step and previous_next_step != next_step:
            changes.append(f"Changed next step from “{previous_next_step[:160]}” to “{next_step[:160]}”.")
        previous_voice = previous_snapshot.get("voice") or ""
        if previous_voice and previous_voice != voice:
            changes.append("Updated Angellos’ voice and reply style.")
        previous_rules = previous_snapshot.get("conversation_rules") or []
        if previous_rules != conversation_rules:
            changes.append("Updated how Angellos qualifies, replies, or guides prospects.")

    if not changes:
        source = clean_text(version.get("source"))
        if source:
            changes.append(f"Created from {version_created_from(version)['label'].lower()}.")

    summary = changes[0] if changes else "This version stores what Angellos knew at that moment."

    return {
        "id": version.get("id"),
        "created_at": version.get("created_at"),
        "is_active": bool(version.get("is_active")),
        "source": version_created_from(version),
        "summary": summary,
        "offer": offer_summary,
        "ideal_customer": ideal_customer,
        "sales_process": sales_process,
        "next_step": next_step,
        "voice": voice,
        "conversation_rules": conversation_rules,
        "forbidden_topics": forbidden_topics,
        "what_changed": changes[:6],
    }


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


def merge_unique_list(existing, incoming) -> list:
    items: list[str] = []
    seen: set[str] = set()
    for value in [*(existing or []), *(incoming or [])]:
        if not isinstance(value, str):
            continue
        cleaned = re.sub(r"\s+", " ", value).strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(cleaned)
    return items


def merge_structured_patch(existing: dict, patch: dict) -> dict:
    merged = dict(existing or {})
    for key, value in clean_json_value(patch or {}).items():
        if isinstance(value, list):
            merged[key] = merge_unique_list(merged.get(key), value)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_structured_patch(merged.get(key) or {}, value)
        elif value not in ("", None, [], {}):
            merged[key] = value
    return clean_json_value(merged)


def extract_text_from_uploaded_knowledge(file_name: str, file_base64: str) -> str:
    if not file_base64:
        return ""
    try:
        raw = base64.b64decode(file_base64, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid uploaded file")
    if len(raw) > 8_000_000:
        raise HTTPException(status_code=413, detail="Uploaded file is too large")

    extension = (file_name.rsplit(".", 1)[-1] if "." in file_name else "").lower()
    if extension in {"txt", "md", "markdown", "csv"}:
        return raw.decode("utf-8", errors="ignore").strip()
    if extension == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                xml = archive.read("word/document.xml")
            root = ET.fromstring(xml)
            paragraphs = [
                node.text.strip()
                for node in root.iter()
                if node.tag.endswith("}t") and node.text and node.text.strip()
            ]
            return "\n".join(paragraphs).strip()
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Unable to read DOCX text: {e}")
    if extension == "pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        except ImportError:
            raise HTTPException(status_code=500, detail="PDF extraction dependency is not installed")
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Unable to read PDF text: {e}")
    raise HTTPException(status_code=415, detail="Unsupported file type")


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


FRENCH_REFINEMENT_MARKERS = [
    "ton rôle",
    "ton role",
    "votre rôle",
    "votre role",
    "rôle :",
    "role :",
    "appel",
    "prospects instagram",
    "page de présentation",
    "ne vends pas",
    "qualifier et orienter",
    "section ciblee",
    "section ciblée",
]


def looks_like_french_refinement_text(value: object) -> bool:
    if isinstance(value, dict):
        return any(looks_like_french_refinement_text(item) for item in value.values())
    if isinstance(value, list):
        return any(looks_like_french_refinement_text(item) for item in value)
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(marker in lowered for marker in FRENCH_REFINEMENT_MARKERS)


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
        "target_section": str(raw_result.get("target_section") or "Target section").strip(),
        "summary": str(raw_result.get("summary") or "").strip(),
        "changes": raw_result.get("changes") if isinstance(raw_result.get("changes"), list) else [],
    }


def prompt_refinement_source(instruction: str) -> str:
    clean_instruction = re.sub(r"\s+", " ", instruction).strip()
    if len(clean_instruction) > 72:
        clean_instruction = f"{clean_instruction[:69]}..."
    return f"training-refine: {clean_instruction}"


def format_training_center_for_prompt(profile: dict, avatar: dict, sales_rules: dict) -> str:
    language = normalize_tenant_language(
        (profile or {}).get("language")
        or (profile or {}).get("preferred_language")
        or (profile or {}).get("default_language")
    )
    parts = [
        "=== TRAINING CENTER ANGELOS ===",
        "This structured data is the business source of truth. Apply it before generic prompt examples.",
        f"Tenant default language: {tenant_language_label(language)}.",
        "Use the tenant default language unless the prospect clearly uses another language first.",
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


PRICE_LEARNING_MARKERS = (
    "ne donne jamais le prix",
    "ne pas donner le prix",
    "pas donner le prix",
    "prix uniquement",
    "prix seulement",
    "appel de vente",
    "sales call",
    "don't give the price",
    "do not give the price",
    "never give the price",
)


def durable_rule_from_refinement_instruction(instruction: str) -> Optional[str]:
    clean_instruction = re.sub(r"\s+", " ", instruction or "").strip()
    lowered = clean_instruction.lower()
    if any(marker in lowered for marker in PRICE_LEARNING_MARKERS):
        return (
            "Ne jamais donner le prix directement en DM. Si le prospect demande le prix, "
            "répondre que le tarif dépend du contexte et qu'il sera confirmé pendant l'audit/appel, "
            "puis poser une question de qualification simple."
        )
    if not clean_instruction:
        return None
    return clean_instruction[:500]


def merge_rule_list(existing: object, rule: str) -> list[str]:
    values = [item for item in (existing if isinstance(existing, list) else []) if isinstance(item, str) and item.strip()]
    normalized_rule = re.sub(r"\s+", " ", rule).strip()
    if not normalized_rule:
        return values
    if not any(item.strip().lower() == normalized_rule.lower() for item in values):
        values.append(normalized_rule)
    return values


async def learn_refinement_rule(user_id: str, instruction: str) -> dict:
    rule = durable_rule_from_refinement_instruction(instruction)
    if not rule:
        return {"learned": False, "rule": ""}

    try:
        sales_rules_row = await get_user_singleton_row(SUPABASE_AGENT_SALES_RULES_URL, user_id)
        rules = dict((sales_rules_row or {}).get("rules") or {})
        profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id)
        profile = (profile_row or {}).get("profile") or {}

        rules["do_not_say"] = merge_rule_list(rules.get("do_not_say"), rule)
        rules["objection_responses"] = merge_rule_list(rules.get("objection_responses"), rule)
        rules["qualification_questions"] = merge_rule_list(
            rules.get("qualification_questions"),
            "Avant de parler tarif, qualifier le type de besoin, la situation actuelle et l'urgence du prospect.",
        )

        await upsert_user_singleton_row(SUPABASE_AGENT_SALES_RULES_URL, user_id, {"rules": clean_json_value(rules)})

        active_prompt = await get_active_prompt(user_id)
        avatar_row = await get_user_singleton_row(SUPABASE_AGENT_AVATARS_URL, user_id)
        avatar = (avatar_row or {}).get("avatar") or {}
        next_prompt = build_training_center_prompt(active_prompt, profile, avatar, rules)

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
                    "source": f"refine-learn: {rule[:72]}",
                    "insight_id": None,
                },
                timeout=10.0,
            )
            res.raise_for_status()
            created = res.json()
            if isinstance(created, list):
                version = created[0] if created else {}
            elif isinstance(created, dict):
                version = created
            else:
                version = {}
        return {"learned": True, "rule": rule, "prompt_version_id": version.get("id")}
    except Exception as e:
        print(f"[refine-learn] failed user_id={user_id}: {e}", flush=True)
        return {"learned": False, "rule": rule, "error": str(e)[:300]}


TRAINING_CENTER_MAIN_STEPS = [
    {"id": "offer", "label": "Your offer"},
    {"id": "knowledge_voice", "label": "Knowledge & voice"},
    {"id": "ideal_customer", "label": "Ideal customer"},
    {"id": "test_angellos", "label": "Test Angellos"},
]


def summarize_agent_sales_rules(sales_rules_row: Optional[dict]) -> list[str]:
    rules = (sales_rules_row or {}).get("rules") or {}
    if not isinstance(rules, dict):
        return []

    summaries: list[str] = []
    summary_fields = [
        ("qualification_questions", "Qualification questions"),
        ("buying_signals", "Buying signals"),
        ("call_offer_conditions", "When to guide toward the next step"),
        ("red_flags", "Bad-fit signals"),
        ("stop_conditions", "When to stop"),
        ("do_not_say", "Phrases to avoid"),
        ("follow_up_rules", "Follow-up style"),
    ]
    for key, label in summary_fields:
        values = rules.get(key)
        if not isinstance(values, list) or not values:
            continue
        visible_values = [
            re.sub(r"\s+", " ", value).strip()
            for value in values
            if isinstance(value, str) and value.strip()
        ][:2]
        if visible_values:
            summaries.append(f"{label}: {'; '.join(visible_values)}")
    return summaries[:6]


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
    if MANUAL_FOLLOW_UP_2_HOURS <= hours_since_user < MANUAL_FOLLOW_UP_3_HOURS:
        return {"stage": "j10", "label": "J+10", "mode": "manual", "sort": 3}
    if hours_since_user >= MANUAL_FOLLOW_UP_3_HOURS:
        return {"stage": "j30", "label": "J+30", "mode": "manual", "sort": 4}
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
    """Return the active prompt from prompt_versions, plus approved review lessons if available."""
    if not user_id:
        return build_system_prompt(config)
    prompt = build_system_prompt(config)
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
                prompt = rows[0]["content"]
    except Exception as e:
        print(f"[get_active_prompt] fallback to hardcoded prompt: {e}")
    return await append_approved_review_lessons(prompt, user_id)


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
        if not rows and channel == "instagram":
            res = await http.get(
                SUPABASE_CONVERSATIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={
                    "username": f"eq.{external_contact_id}",
                    "user_id": f"eq.{user_id}",
                    "limit": 1,
                    "order": "created_at.desc",
                },
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
    safe_display_name = normalize_display_name(display_name, external_contact_id=external_contact_id)
    username = safe_display_name if channel == "instagram" and is_real_instagram_username(safe_display_name) else external_contact_id
    default_automation_mode = default_automation_mode_for_prompt(await get_active_prompt(user_id))
    row = {
        "username": username,
        "display_name": safe_display_name,
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
        "automation_mode": default_automation_mode,
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


async def fetch_prompt_version_for_memory(version_id: str, user_id: str) -> dict:
    async with httpx.AsyncClient() as http:
        params = {
            "id": f"eq.{version_id}",
            **owner_scope(user_id),
            "select": "id,created_at,is_active,source,insight_id,content,refinement_instruction,refinement_applied_at,previous_version_id,prompt_diff",
            "limit": "1",
        }
        try:
            res = await http.get(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params=params,
                timeout=10.0,
            )
            res.raise_for_status()
        except httpx.HTTPStatusError as select_error:
            if not is_supabase_schema_cache_error(select_error):
                raise
            print(
                "[prompt-version-memory:schema-fallback] "
                f"status={select_error.response.status_code} body={select_error.response.text[:1000]}"
            )
            res = await http.get(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={
                    "id": f"eq.{version_id}",
                    **owner_scope(user_id),
                    "select": "id,created_at,is_active,source,insight_id,content",
                    "limit": "1",
                },
                timeout=10.0,
            )
            res.raise_for_status()
        rows = res.json()
    if not rows:
        raise HTTPException(status_code=404, detail="Prompt version not found")
    return rows[0]


async def fetch_previous_prompt_version_for_memory(version: dict, user_id: str) -> Optional[dict]:
    previous_id = clean_text(version.get("previous_version_id"))
    if previous_id:
        try:
            return await fetch_prompt_version_for_memory(previous_id, user_id)
        except Exception as e:
            print(f"[prompt-version-memory] previous_id lookup failed: {e}")

    created_at = clean_text(version.get("created_at"))
    if not created_at:
        return None
    try:
        async with httpx.AsyncClient() as http:
            res = await http.get(
                SUPABASE_PROMPT_VERSIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={
                    "created_at": f"lt.{created_at}",
                    **owner_scope(user_id),
                    "order": "created_at.desc",
                    "select": "id,created_at,is_active,source,insight_id,content",
                    "limit": "1",
                },
                timeout=10.0,
            )
            res.raise_for_status()
            rows = res.json()
            return rows[0] if rows else None
    except Exception as e:
        print(f"[prompt-version-memory] previous version lookup failed: {e}")
        return None


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
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
        )
        return response.content[0].text
    except ProviderGenerationError:
        raise
    except Exception as e:
        raise classify_provider_error(e) from e


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


async def fetch_manychat_ig_username(subscriber_id: str) -> Optional[str]:
    """Call ManyChat getInfo to resolve the real Instagram username when the webhook field is missing."""
    if not MANYCHAT_API_KEY or not subscriber_id:
        return None
    try:
        async with httpx.AsyncClient() as http:
            res = await http.get(
                "https://api.manychat.com/fb/subscriber/getInfo",
                headers={"Authorization": f"Bearer {MANYCHAT_API_KEY}"},
                params={"subscriber_id": subscriber_id},
                timeout=8.0,
            )
            if res.status_code != 200:
                print(f"[manychat:getInfo] status={res.status_code} subscriber_id={subscriber_id}")
                return None
            ig_username = extract_manychat_ig_username(res.json())
            if ig_username:
                print(f"[manychat:getInfo] resolved ig_username={ig_username!r} for subscriber_id={subscriber_id}")
                return ig_username
            print(f"[manychat:getInfo] no real instagram username found for subscriber_id={subscriber_id}")
    except Exception as e:
        print(f"[manychat:getInfo] error subscriber_id={subscriber_id}: {e}")
    return None


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


def is_manychat_pending_delivery_error(send_result: Optional[dict]) -> bool:
    """Return True when ManyChat/Meta reports error 3011 (24h window closed)."""
    if not send_result:
        return False
    if send_result.get("status_code") == 3011:
        return True
    body = send_result.get("body") or ""
    try:
        parsed = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError:
        parsed = None

    def contains_3011(value: object) -> bool:
        if isinstance(value, dict):
            if str(value.get("code") or value.get("error_code") or "") == "3011":
                return True
            return any(contains_3011(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_3011(item) for item in value)
        return str(value).strip() == "3011"

    return contains_3011(parsed) if parsed is not None else "3011" in str(body)


def pending_delivery_indices(history: list) -> list[int]:
    return [
        index
        for index, message in enumerate(history)
        if message.get("role") == "assistant"
        and not message.get("sent")
        and not message.get("ignored")
        and (message.get("pending_delivery") or message.get("delivery_failed"))
        and message.get("content")
    ]


def pending_delivery_status(history: list) -> tuple[bool, Optional[str], Optional[str]]:
    indices = pending_delivery_indices(history)
    if not indices:
        return False, None, None
    last_pending = history[indices[-1]]
    return True, last_pending.get("content"), last_pending.get("timestamp") or now_iso()


async def flush_pending_deliveries(conversation: dict, user_id: str) -> dict:
    """Retry unsent pending-delivery assistant messages on the next inbound message."""
    history = [dict(message) for message in (conversation.get("history") or [])]
    indices = pending_delivery_indices(history)
    if not indices:
        return {"history": history, "flushed": 0, "failed": 0, "attempted": 0}

    flushed = 0
    failed = 0
    attempted = 0
    for index in indices:
        message = history[index]
        attempted += 1
        send_result = await send_channel_message(conversation, message.get("content") or "")
        message["send_status_code"] = send_result.get("status_code")
        message["delivery_retry_at"] = now_iso()
        if send_result.get("status_code", 500) < 400:
            message["sent"] = True
            message["pending_delivery"] = False
            message["delivery_failed"] = False
            message["delivery_status"] = "sent_after_inbound_retry"
            flushed += 1
            continue
        failed += 1
        message["sent"] = False
        message["pending_delivery"] = True
        message["delivery_failed"] = True
        message["delivery_status"] = "pending_delivery" if is_manychat_pending_delivery_error(send_result) else "send_failed"
        message["send_error_body"] = (send_result.get("body") or "")[:500]
        break

    has_pending, pending_message, pending_message_at = pending_delivery_status(history)
    patch_data = {
        "history": history,
        "status": "pending_delivery" if has_pending else "en_cours",
        "pending_message": pending_message,
        "pending_message_at": pending_message_at,
    }
    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation.get('id')}", "user_id": f"eq.{user_id}"},
            json=patch_data,
            timeout=10.0,
        )
        res.raise_for_status()
    print(
        f"[pending-delivery] attempted={attempted} flushed={flushed} failed={failed} "
        f"conversation_id={conversation.get('id')}",
        flush=True,
    )
    return {"history": history, "flushed": flushed, "failed": failed, "attempted": attempted}


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


def split_stop_agent_reply(reply: str) -> tuple[str, bool, bool]:
    """
    Parse reply for [STOP_AGENT] and [HUMAN_MODE] tokens.
    Returns (cleaned_reply, should_stop_agent, should_human_mode).
    Both tokens are stripped from the returned reply.
    """
    should_stop_agent = False
    should_human_mode = False

    if not reply:
        return reply, False, False

    # Handle [STOP_AGENT]
    if "[STOP_AGENT]" in reply:
        before_tag = reply.split("[STOP_AGENT]", 1)[0].strip()
        if len(before_tag) >= 3:
            reply = before_tag
        else:
            reply = ""
        should_stop_agent = True

    # Handle [HUMAN_MODE] — strip it from the reply
    if "[HUMAN_MODE]" in reply:
        reply = reply.replace("[HUMAN_MODE]", "").strip()
        should_human_mode = True

    return reply, should_stop_agent, should_human_mode


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
    is_pending_delivery = is_manychat_pending_delivery_error(send_result)
    for message in reversed(history):
        if not patched and message.get("role") == "assistant" and message.get("source") == "inbound_auto":
            updated = dict(message)
            updated["sent"] = sent
            if send_result is not None:
                updated["send_status_code"] = send_result.get("status_code")
            if sent:
                updated["pending_delivery"] = False
                updated["delivery_failed"] = False
                updated["delivery_status"] = "sent"
            elif is_pending_delivery:
                updated["pending_delivery"] = True
                updated["delivery_failed"] = True
                updated["delivery_status"] = "pending_delivery"
                updated["send_error_body"] = (send_result.get("body") or "")[:500] if send_result else ""
            else:
                updated["delivery_status"] = "send_failed"
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
        "j30": "assisted D+30 follow-up",
    }
    stage_label = stage_labels.get(stage, stage)
    active_prompt = await get_active_prompt(conversation.get("user_id"))
    generation_prompt = build_generation_prompt(active_prompt)
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
            generation_prompt,
        )
    except ProviderGenerationError as e:
        raise HTTPException(status_code=e.status_code, detail=provider_error_payload(e))

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


def _clamp_score(value: object) -> int:
    try:
        score = int(str(value))
    except (TypeError, ValueError):
        score = 1
    return max(1, min(score, 10))


def normalize_conversation_review(raw_review: dict, conversation: dict) -> dict:
    allowed_categories = {
        "too_robotic",
        "too_long",
        "too_commercial",
        "bad_emotional_read",
        "bad_question",
        "missed_context",
        "should_have_handed_off",
        "objective_reached",
        "other",
    }
    objective_reached = bool(raw_review.get("objective_reached"))
    failure_category = clean_text(raw_review.get("failure_category")) or "other"
    if failure_category not in allowed_categories:
        failure_category = "other"
    if objective_reached and failure_category in {"", "other"}:
        failure_category = "objective_reached"

    return {
        "conversation_id": conversation.get("id"),
        "username": conversation.get("display_name") or conversation.get("username") or "unknown",
        "objective_reached": objective_reached,
        "objective_reason": clean_text(raw_review.get("objective_reason")),
        "human_likeness_score": _clamp_score(raw_review.get("human_likeness_score")),
        "sales_effectiveness_score": _clamp_score(raw_review.get("sales_effectiveness_score")),
        "engagement_score": _clamp_score(raw_review.get("engagement_score")),
        "moment_of_failure": clean_text(raw_review.get("moment_of_failure")) or ("none" if objective_reached else "unspecified"),
        "failure_category": failure_category,
        "what_angellos_did_wrong": clean_text(raw_review.get("what_angellos_did_wrong")),
        "better_human_reply": clean_text(raw_review.get("better_human_reply")),
        "lesson_learned": clean_text(raw_review.get("lesson_learned")),
        "prompt_rule_candidate": clean_text(raw_review.get("prompt_rule_candidate")),
    }


def conversation_last_activity_at(conversation: dict) -> Optional[datetime]:
    timestamps = [
        parse_iso(conversation.get("updated_at")),
        parse_iso(conversation.get("last_inbound_at")),
        parse_iso(conversation.get("created_at")),
    ]
    for message in conversation.get("history") or []:
        timestamps.append(get_message_time(message))
    present = [value for value in timestamps if value is not None]
    return max(present) if present else None


def conversation_review_user_message(conversation: dict, active_prompt: str) -> str:
    history = conversation.get("history") or []
    lines = [
        "Review this complete Angellos conversation.",
        "",
        "=== ACTIVE ANGELLOS PROMPT SUMMARY SOURCE ===",
        active_prompt[:12000],
        "",
        "=== CONVERSATION METADATA ===",
        f"conversation_id: {conversation.get('id')}",
        f"username: {conversation.get('display_name') or conversation.get('username')}",
        f"status: {conversation.get('status')}",
        f"automation_mode: {conversation.get('automation_mode')}",
        f"message_count: {len(history)}",
        "",
        "=== COMPLETE MESSAGE HISTORY ===",
    ]
    for index, message in enumerate(history, 1):
        role = "Prospect" if message.get("role") == "user" else "Angellos"
        timestamp = message.get("timestamp") or message.get("created_at") or "unknown_time"
        sent = message.get("sent")
        ignored = message.get("ignored")
        metadata = []
        if sent is not None:
            metadata.append(f"sent={sent}")
        if ignored:
            metadata.append("ignored=true")
        suffix = f" ({', '.join(metadata)})" if metadata else ""
        lines.append(f"{index}. [{timestamp}] {role}{suffix}: {message.get('content', '')}")
    return "\n".join(lines)


def review_public_payload(review: dict) -> dict:
    keys = [
        "id",
        "created_at",
        "review_date",
        "conversation_id",
        "username",
        "objective_reached",
        "objective_reason",
        "human_likeness_score",
        "sales_effectiveness_score",
        "engagement_score",
        "moment_of_failure",
        "failure_category",
        "what_angellos_did_wrong",
        "better_human_reply",
        "lesson_learned",
        "prompt_rule_candidate",
        "lesson_status",
    ]
    return {key: review.get(key) for key in keys if key in review}


async def get_approved_conversation_review_lessons(user_id: str, limit: int = 8) -> list[str]:
    try:
        async with httpx.AsyncClient() as http:
            res = await http.get(
                SUPABASE_CONVERSATION_REVIEWS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params={
                    "user_id": f"eq.{user_id}",
                    "lesson_status": "eq.approved",
                    "prompt_rule_candidate": "not.is.null",
                    "order": "approved_at.desc,created_at.desc",
                    "limit": str(limit),
                    "select": "prompt_rule_candidate",
                },
                timeout=5.0,
            )
            res.raise_for_status()
            rows = res.json()
    except Exception as e:
        print(f"[review-lessons] approved lesson lookup failed: {e}")
        return []
    lessons = []
    for row in rows:
        lesson = clean_text(row.get("prompt_rule_candidate"))
        if lesson:
            lessons.append(lesson)
    return lessons[:limit]


async def append_approved_review_lessons(prompt: str, user_id: Optional[str]) -> str:
    if not user_id:
        return prompt
    lessons = await get_approved_conversation_review_lessons(user_id)
    if not lessons:
        return prompt
    lines = [
        prompt,
        "",
        "=== APPROVED CONVERSATION REVIEW LESSONS ===",
        "Apply these approved lessons when they fit the conversation. Do not overrule higher-priority business rules or safety constraints.",
    ]
    lines.extend(f"- {lesson}" for lesson in lessons)
    return "\n".join(lines)


async def fetch_conversations_for_review(user_id: str, start: datetime, end: datetime, limit: int, conversation_id: Optional[str] = None) -> list[dict]:
    params = {
        "user_id": f"eq.{user_id}",
        "order": "created_at.desc",
        "limit": str(min(max(limit * 3, limit), 500)),
        "select": "id,created_at,updated_at,username,display_name,message,status,agent_active,automation_mode,history,channel,external_contact_id,last_inbound_at",
    }
    if conversation_id:
        params["id"] = f"eq.{conversation_id}"
    async with httpx.AsyncClient() as http:
        try:
            res = await http.get(
                SUPABASE_CONVERSATIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params=params,
                timeout=15.0,
            )
            res.raise_for_status()
        except httpx.HTTPStatusError as select_error:
            if not is_supabase_schema_cache_error(select_error):
                raise
            fallback_params = dict(params)
            fallback_params["select"] = "id,created_at,username,display_name,message,status,agent_active,automation_mode,history,channel,external_contact_id,last_inbound_at"
            res = await http.get(
                SUPABASE_CONVERSATIONS_URL,
                headers={**supabase_headers(), "Accept": "application/json"},
                params=fallback_params,
                timeout=15.0,
            )
            res.raise_for_status()
        rows = res.json()
    eligible = []
    for conversation in rows:
        last_activity_at = conversation_last_activity_at(conversation)
        if conversation_id or (last_activity_at and start <= last_activity_at < end):
            conversation["_last_activity_at"] = last_activity_at.isoformat() if last_activity_at else None
            eligible.append(conversation)
    return eligible[:limit]


async def review_single_conversation(conversation: dict, active_prompt: str) -> dict:
    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=build_conversation_review_prompt(config),
        messages=[{"role": "user", "content": conversation_review_user_message(conversation, active_prompt)}],
    )
    raw = getattr(response.content[0], "text", "").strip()
    return normalize_conversation_review(parse_llm_json(raw), conversation)


async def store_conversation_review(user_id: str, review_date: str, conversation: dict, review: dict) -> dict:
    row = {
        **row_owner_fields(user_id),
        "review_date": review_date,
        "conversation_id": review["conversation_id"],
        "username": review["username"],
        "conversation_updated_at": conversation.get("_last_activity_at") or conversation.get("last_inbound_at") or conversation.get("created_at"),
        "message_count": len(conversation.get("history") or []),
        "objective_reached": review["objective_reached"],
        "objective_reason": review["objective_reason"],
        "human_likeness_score": review["human_likeness_score"],
        "sales_effectiveness_score": review["sales_effectiveness_score"],
        "engagement_score": review["engagement_score"],
        "moment_of_failure": review["moment_of_failure"],
        "failure_category": review["failure_category"],
        "what_angellos_did_wrong": review["what_angellos_did_wrong"],
        "better_human_reply": review["better_human_reply"],
        "lesson_learned": review["lesson_learned"],
        "prompt_rule_candidate": review["prompt_rule_candidate"],
        "lesson_status": "candidate",
        "reviewer_model": "claude-sonnet-4-6",
        "raw_review": review,
    }
    async with httpx.AsyncClient() as http:
        res = await http.post(
            SUPABASE_CONVERSATION_REVIEWS_URL,
            headers={
                **supabase_headers(),
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
            params={"on_conflict": "user_id,conversation_id,review_date"},
            json=row,
            timeout=10.0,
        )
        res.raise_for_status()
        created = res.json()
    if isinstance(created, list):
        return created[0] if created else row
    if isinstance(created, dict):
        return created
    return row


async def run_daily_conversation_review_job(user_id: str, review_date: Optional[str] = None, limit: int = 200, conversation_id: Optional[str] = None) -> dict:
    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")
    selected_date = review_date or datetime.now(timezone.utc).date().isoformat()
    try:
        start = datetime.fromisoformat(selected_date).replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=422, detail="review_date must be YYYY-MM-DD")
    end = start + timedelta(days=1)
    bounded_limit = min(max(limit, 1), 200)
    conversations = await fetch_conversations_for_review(user_id, start, end, bounded_limit, conversation_id)
    if not conversations:
        return {
            "success": True,
            "review_date": selected_date,
            "selected": 0,
            "stored": 0,
            "reviews": [],
            "lesson_injection": "Only reviews with lesson_status=approved are appended to Angellos prompt context.",
        }

    active_prompt = await get_active_prompt(user_id)
    stored_reviews = []
    errors = []
    for conversation in conversations:
        try:
            review = await review_single_conversation(conversation, active_prompt)
            stored = await store_conversation_review(user_id, selected_date, conversation, review)
            stored_reviews.append(review_public_payload(stored))
        except Exception as e:
            print(f"[reviews:daily] failed conversation_id={conversation.get('id')} error={e}")
            errors.append({"conversation_id": conversation.get("id"), "error": str(e)[:300]})

    return {
        "success": len(errors) == 0,
        "review_date": selected_date,
        "selected": len(conversations),
        "stored": len(stored_reviews),
        "errors": errors,
        "reviews": stored_reviews,
        "lesson_injection": "Only reviews with lesson_status=approved are appended to Angellos prompt context.",
    }


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


class DailyReviewPayload(BaseModel):
    user_id: str = Field(max_length=200)
    review_date: Optional[str] = Field(default=None, max_length=10)
    limit: int = 200
    conversation_id: Optional[str] = Field(default=None, max_length=200)


class ReviewLessonStatusPayload(BaseModel):
    lesson_status: str


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
    language: str = Field(default="en", max_length=20)
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
    sales_process: str = Field(default="", max_length=20000)
    next_step: str = Field(default="", max_length=5000)
    voice_profile: str = Field(default="", max_length=10000)
    knowledge_sources: list[str] = Field(default_factory=list)


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


class KnowledgeExtractPayload(BaseModel):
    manual_process: str = Field(default="", max_length=20000)
    pasted_text: str = Field(default="", max_length=120000)
    file_name: str = Field(default="", max_length=300)
    file_type: str = Field(default="", max_length=120)
    file_base64: str = Field(default="", max_length=12_000_000)
    category: str = Field(default="mixed", max_length=120)


class KnowledgeTrainPayload(BaseModel):
    profile_patch: dict = Field(default_factory=dict)
    avatar_patch: dict = Field(default_factory=dict)
    rules_patch: dict = Field(default_factory=dict)


class FollowUpPreviewPayload(BaseModel):
    conversation_id: str
    stage: str


class ManyChatFollowUpPayload(BaseModel):
    subscriber_id: str


class AutomationModePayload(BaseModel):
    automation_mode: str  # "auto" | "supervised" | "disabled"


class BulkAutomationModePayload(BaseModel):
    automation_mode: str = "auto"


class RefineMessagePayload(BaseModel):
    instruction: str = Field(max_length=1000)
    original_message: str = Field(default="", max_length=4000)
    learn: bool = True


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
    existing_display_name = (contact.get("display_name") or "") if contact else ""
    safe_display_name = normalize_display_name(display_name, existing_display_name, external_contact_id)
    if contact is None:
        contact = await create_contact(
            external_contact_id=external_contact_id,
            display_name=safe_display_name,
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

    pending_flush = await flush_pending_deliveries(contact, user_id)
    if pending_flush.get("attempted"):
        history = pending_flush.get("history") or history
        contact = {**contact, "history": history}

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
    if safe_display_name:
        patch_data["display_name"] = safe_display_name
        if channel == "instagram" and is_real_instagram_username(safe_display_name):
            patch_data["username"] = safe_display_name
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

    try:
        await enforce_ai_cost_cap(user_id)
    except CostCapExceededError as e:
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
        return {
            "reply": "",
            "sent": False,
            "should_send": False,
            "mode": automation_mode,
            "skipped": True,
            "reason": "ai_cost_cap_reached",
            "error": cost_cap_error_payload(e),
            "conversation_id": contact.get("id"),
        }

    active_prompt = await get_active_prompt(contact.get("user_id"))
    system_prompt = build_generation_prompt(active_prompt)
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

    should_human_mode = False
    canned_reply = get_angellos_beta_canned_reply(message) if is_angellos_acquisition_prompt(active_prompt) else None
    if canned_reply:
        reply, should_stop_agent = canned_reply
    else:
        first_turn = not history
        prospect_label = "Prospect WhatsApp" if channel == "whatsapp" else "Prospect Instagram"
        user_content = (
            f"{prospect_label}: {safe_display_name}\nReceived message: {message}"
            if first_turn
            else message
        )
        messages_for_generation = history + [{"role": "user", "content": user_content, "timestamp": received_at}]

        try:
            reply = generate_claude_reply(strip_message_metadata(messages_for_generation), system_prompt)
        except ProviderGenerationError as e:
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
            if channel == "instagram":
                await clear_manychat_agent_response(external_contact_id)
            print(f"[inbound] PROVIDER_ERROR channel={channel} external_id={external_contact_id} type={e.error_type}")
            return {
                "reply": "",
                "sent": False,
                "should_send": False,
                "mode": automation_mode,
                "skipped": True,
                "reason": e.error_type,
                "error": provider_error_payload(e),
                "conversation_id": contact.get("id"),
            }
        reply, should_stop_agent, should_human_mode = split_stop_agent_reply(reply)
    reply = sanitize_angellos_beta_reply(reply, message)
    reply = validate_agent_reply(reply, system_prompt)
    await record_ai_usage_event(user_id, "inbound_reply", json.dumps(strip_message_metadata(messages_for_generation), ensure_ascii=False) if not canned_reply else message, reply)

    if should_stop_agent:
        if not (channel == "whatsapp" and (is_whatsapp_test_contact(contact) or is_whatsapp_test_metadata(transport_metadata))):
            patch_data["agent_active"] = False
            print(f"[inbound] STOP_AGENT channel={channel} external_id={external_contact_id}")
        else:
            print(f"[inbound] STOP_AGENT ignored for whatsapp test contact external_id={external_contact_id}")

    if should_human_mode:
        patch_data["automation_mode"] = "disabled"
        print(f"[inbound] HUMAN_MODE channel={channel} external_id={external_contact_id} -> automation_mode=disabled")

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
        is_pending_delivery = is_manychat_pending_delivery_error(send_result)
        sent_history = mark_last_auto_assistant_sent(new_history, sent, send_result)
        followup_patch = {
            "history": sent_history,
            "pending_message": None if sent else reply,
            "pending_message_at": None if sent else now_iso(),
        }
        if is_pending_delivery:
            followup_patch["status"] = "pending_delivery"
        elif sent:
            followup_patch["status"] = "en_cours"
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
            print(
                f"[inbound] SEND_FAILED channel={channel} external_id={external_contact_id} "
                f"status={send_result['status_code']}",
                flush=True,
            )
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

    # Resolve display name: if {{ig_username}} didn't resolve or sent a numeric ID,
    # fall back to ManyChat's getInfo API which always has the real Instagram handle.
    display_name = payload.username
    if is_placeholder_display_name(display_name):
        fetched = await fetch_manychat_ig_username(payload.subscriber_id)
        if fetched:
            display_name = fetched

    result = await handle_inbound_message(
        channel="instagram",
        external_contact_id=payload.subscriber_id,
        display_name=display_name,
        message=payload.message,
        user_id=user_id,
        transport_metadata={
            "provider": "manychat",
            "subscriber_id": payload.subscriber_id,
            "webhook_username": payload.username,
        },
        auto_send_transport=True,
    )
    should_send = bool(result.get("should_send"))
    sent_by_backend = bool(result.get("sent"))
    mode = result.get("mode") or "supervised"
    public_mode = "off" if mode == "disabled" else mode
    reply = result.get("reply") or ""
    return {
        "agent_response": reply if should_send and not sent_by_backend else "",
        "suggested_response": reply if mode == "supervised" else "",
        "should_send": should_send,
        "sent": sent_by_backend,
        "mode": public_mode,
        "automation_mode": mode,
        "reason": result.get("reason"),
        "ok": not bool(result.get("error")),
        "error": result.get("error"),
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


def _last_prospect_message(history: list) -> Optional[dict]:
    """Return the most recent user-role message, or None."""
    for msg in reversed(history):
        if msg.get("role") == "user":
            return msg
    return None


def _needs_supervised_pending(conversation: dict) -> bool:
    """True when activation should generate a supervised pending reply."""
    if conversation.get("automation_mode") != "supervised":
        return False
    if conversation.get("pending_message"):
        return False
    history = conversation.get("history") or []
    if not history:
        return False
    last = history[-1]
    # Any assistant message (sent or unsent) that isn't explicitly ignored means
    # the prospect's last message has already been handled.
    if last.get("role") == "assistant" and not last.get("ignored"):
        return False
    return _last_prospect_message(history) is not None


async def _generate_and_save_supervised_pending(
    conversation: dict,
    conversation_id: str,
    user_id: str,
) -> Optional[str]:
    """Generate a supervised reply for the unanswered prospect message and persist it.

    Returns the generated reply text, or None if generation was skipped or failed.
    Errors from the AI provider are allowed to propagate so the caller can decide
    whether to treat them as fatal.
    """
    if client is None:
        return None
    history = conversation.get("history") or []
    last_user_msg = _last_prospect_message(history)
    if last_user_msg is None:
        return None

    active_prompt = await get_active_prompt(user_id)
    system_prompt = build_generation_prompt(active_prompt)

    # Build message list up to and including the last user message
    messages_for_gen: list[dict] = []
    for msg in history:
        messages_for_gen.append(msg)
        if msg is last_user_msg:
            break

    try:
        reply = generate_claude_reply(strip_message_metadata(messages_for_gen), system_prompt)
    except ProviderGenerationError as e:
        print(
            f"[generate-pending:claude] error type={e.error_type} message={e.message} "
            f"conversation_id={conversation_id}",
            flush=True,
        )
        raise

    reply, _, should_human_mode = split_stop_agent_reply(reply)
    reply = sanitize_angellos_beta_reply(reply, last_user_msg.get("content", ""))
    reply = validate_agent_reply(reply, system_prompt)

    now = now_iso()
    assistant_entry = {
        "role": "assistant",
        "content": reply,
        "timestamp": now,
        "sent": False,
        "ignored": False,
        "source": "activation_supervised",
    }
    new_history = history + [assistant_entry]
    if len(new_history) > MAX_HISTORY_TURNS * 2:
        new_history = new_history[-(MAX_HISTORY_TURNS * 2):]

    patch_body = {
        "pending_message": reply,
        "pending_message_at": now,
        "history": new_history,
    }
    if should_human_mode:
        patch_body["automation_mode"] = "disabled"
        print(f"[generate-pending] HUMAN_MODE conversation_id={conversation_id} -> automation_mode=disabled")

    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{conversation_id}", "user_id": f"eq.{user_id}"},
            json=patch_body,
            timeout=10.0,
        )
        res.raise_for_status()

    print(f"[activate] supervised pending generated conversation_id={conversation_id}")
    return reply


@app.post("/conversations/{conversation_id}/activate")
async def activate_conversation(
    conversation_id: str,
    user_id: str = Depends(require_jwt),
):
    conversation = await get_conversation_by_id(conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    history = conversation.get("history") or []
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

    pending_generated = False
    pending_message: Optional[str] = None

    # Re-read conversation state with agent_active=True in mind for the check
    activated_conversation = {**conversation, "agent_active": True}
    if _needs_supervised_pending(activated_conversation):
        try:
            pending_message = await _generate_and_save_supervised_pending(
                conversation, conversation_id, user_id
            )
            pending_generated = pending_message is not None
        except ProviderGenerationError as e:
            print(f"[activate] supervised pending generation failed: {e.error_type} {e.message}")
        except Exception as e:
            print(f"[activate] supervised pending generation error: {e}")

    return {
        "success": True,
        "pending_generated": pending_generated,
        "pending_message": pending_message,
    }


@app.post("/conversations/{conversation_id}/generate-pending")
async def generate_pending(
    conversation_id: str,
    user_id: str = Depends(require_jwt),
):
    """Generate or regenerate a supervised pending reply for the latest unanswered prospect message."""
    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    conversation = await get_conversation_by_id(conversation_id, user_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if not conversation.get("agent_active"):
        raise HTTPException(status_code=409, detail="Agent is not active for this conversation")

    if conversation.get("automation_mode") != "supervised":
        raise HTTPException(status_code=409, detail="generate-pending only applies to supervised mode")

    if not _last_prospect_message(conversation.get("history") or []):
        raise HTTPException(status_code=409, detail="No prospect message to reply to")

    try:
        pending_message = await _generate_and_save_supervised_pending(
            conversation, conversation_id, user_id
        )
    except ProviderGenerationError as e:
        print(
            f"[generate-pending] provider error"
            f" type={e.error_type}"
            f" status={e.status_code}"
            f" message={e.message}"
            f" conversation_id={conversation_id}",
            flush=True,
        )
        return provider_error_response(e)

    if not pending_message:
        print(f"[generate-pending] no reply generated conversation_id={conversation_id}", flush=True)
        raise HTTPException(status_code=409, detail="Could not generate a reply")

    print(f"[generate-pending] success conversation_id={conversation_id}", flush=True)
    return {"success": True, "pending_message": pending_message}


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


@app.post("/conversations/bulk-automation-mode")
async def bulk_update_automation_mode(
    payload: BulkAutomationModePayload = BulkAutomationModePayload(),
    user_id: str = Depends(require_jwt),
):
    target_mode = payload.automation_mode
    if target_mode != "auto":
        raise HTTPException(status_code=400, detail="Bulk action only supports switching supervised conversations to auto")

    async with httpx.AsyncClient() as http:
        read_res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={
                "user_id": f"eq.{user_id}",
                "select": "id,automation_mode",
                "limit": "10000",
            },
            timeout=10.0,
        )
        read_res.raise_for_status()
        conversations = read_res.json()

        eligible_ids = [row["id"] for row in conversations if (row.get("automation_mode") or "supervised") == "supervised"]
        skipped_off_disabled = sum(1 for row in conversations if (row.get("automation_mode") or "supervised") in {"disabled", "off", "paused"})
        skipped_other = max(0, len(conversations) - len(eligible_ids) - skipped_off_disabled)

        switched = 0
        failed = 0
        failed_ids: list[str] = []
        for conversation_id in eligible_ids:
            try:
                patch_res = await http.patch(
                    SUPABASE_CONVERSATIONS_URL,
                    headers={**supabase_headers(), "Prefer": "return=minimal"},
                    params={
                        "id": f"eq.{conversation_id}",
                        "user_id": f"eq.{user_id}",
                        "automation_mode": "eq.supervised",
                    },
                    json={"automation_mode": "auto"},
                    timeout=10.0,
                )
                patch_res.raise_for_status()
                switched += 1
            except Exception as e:
                failed += 1
                failed_ids.append(conversation_id)
                print(f"[bulk-auto] failed conversation_id={conversation_id}: {type(e).__name__}: {e}", flush=True)

    return {
        "success": failed == 0,
        "target_mode": "auto",
        "switched_to_auto": switched,
        "skipped_off_disabled": skipped_off_disabled,
        "skipped_other": skipped_other,
        "failed": failed,
        "failed_ids": failed_ids[:20],
    }


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
    generation_prompt = build_generation_prompt(active_prompt)
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
    except ProviderGenerationError as e:
        return provider_error_response(e)

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

    learning_result = {"learned": False, "rule": ""}
    if payload.learn:
        learning_result = await learn_refinement_rule(user_id, instruction)

    print(
        f"[refine-pending] conversation_id={conversation_id} instruction_len={len(instruction)} "
        f"learned={learning_result.get('learned')}",
        flush=True,
    )
    return {"refined_message": refined, "learning": learning_result}


@app.post("/conversations/seed")
async def seed_conversation(
    body: dict,
    x_dashboard_secret: Optional[str] = Header(default=None),
) -> dict:
    require_dashboard_secret(x_dashboard_secret)

    username = (body.get("username") or "").strip().lower()
    first_dm = (body.get("first_dm") or "").strip()
    user_id = (body.get("user_id") or "").strip()

    if not username or not first_dm:
        raise HTTPException(status_code=422, detail="username and first_dm are required")
    if not user_id:
        user_id = "default"

    existing = await get_contact_by_external_id(username, "instagram", user_id)
    if existing:
        return {"status": "already_exists", "conversation_id": existing.get("id")}

    default_automation_mode = default_automation_mode_for_prompt(await get_active_prompt(user_id))
    now = now_iso()
    row = {
        "username": username,
        "display_name": username,
        "message": first_dm[:200],
        "status": "nouveau",
        "agent_active": True,
        "history": [
            {"role": "assistant", "content": first_dm, "timestamp": now, "sent": True}
        ],
        "channel": "instagram",
        "external_contact_id": username,
        "user_id": user_id,
        "automation_mode": default_automation_mode,
        "last_inbound_at": None,
    }

    async with httpx.AsyncClient() as http:
        res = await http.post(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Prefer": "return=representation"},
            json=row,
            timeout=10.0,
        )
        if res.status_code >= 400:
            print(f"[seed_conversation] Supabase error status={res.status_code} body={res.text!r}")
            raise HTTPException(status_code=502, detail=f"Supabase error: {res.text[:200]}")
        created = res.json()

    conversation_id = None
    if isinstance(created, list) and created:
        conversation_id = created[0].get("id")
    elif isinstance(created, dict):
        conversation_id = created.get("id")

    print(f"[seed_conversation] Created conversation {conversation_id} for @{username}")
    return {"status": "created", "conversation_id": conversation_id}


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


@app.get("/beta/ai-cost")
async def get_beta_ai_cost(user_id: str = Depends(require_jwt)):
    settings = await get_beta_cost_settings(user_id)
    spent = await get_estimated_ai_spend_eur(user_id)
    cap = float(settings["cap_eur"])
    return {
        "spent_eur": round(spent, 4),
        "cap_eur": round(cap, 2),
        "remaining_eur": round(max(0.0, cap - spent), 4),
        "guardrail_enabled": settings.get("enabled", True),
        "cap_reached": settings.get("enabled", True) and spent >= cap,
        "pricing_assumption": {
            "model": "claude-sonnet-4-6",
            "input_eur_per_million_tokens": CLAUDE_SONNET_4_6_INPUT_EUR_PER_MTOKEN,
            "output_eur_per_million_tokens": CLAUDE_SONNET_4_6_OUTPUT_EUR_PER_MTOKEN,
            "token_estimation": "ceil(characters / 4) when provider usage is not persisted",
        },
    }


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
                "select": "id,created_at,user_id,username,display_name,message,status,agent_active,automation_mode,history,channel,external_contact_id,phone_e164,last_inbound_at",
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
    x_webhook_secret: Optional[str] = Header(default=None),
):
    user_id = await require_secret(x_webhook_secret)

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

    try:
        await enforce_ai_cost_cap(user_id)
    except CostCapExceededError as e:
        raise HTTPException(status_code=402, detail=cost_cap_error_payload(e))
    message = await generate_follow_up_message(conversation, "auto_23h")
    await record_ai_usage_event(user_id, "follow_up_auto_23h", json.dumps(conversation.get("history") or [], ensure_ascii=False), message)
    send_result = await send_channel_message(conversation, message)
    is_pending_delivery = is_manychat_pending_delivery_error(send_result)
    if send_result["status_code"] >= 400 and not is_pending_delivery:
        raise HTTPException(status_code=502, detail=f"Send error: {send_result['body']}")

    history = conversation.get("history") or []
    sent = send_result["status_code"] < 400
    new_history = history + [{
        "role": "assistant",
        "content": message,
        "timestamp": now_iso(),
        "follow_up_stage": "auto_23h",
        "follow_up_mode": "auto",
        "source": "follow_up_auto",
        "sent": sent,
        "pending_delivery": is_pending_delivery,
        "delivery_failed": is_pending_delivery,
        "delivery_status": "pending_delivery" if is_pending_delivery else "sent",
        "send_status_code": send_result.get("status_code"),
        **({"send_error_body": (send_result.get("body") or "")[:500]} if is_pending_delivery else {}),
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
                "status": "pending_delivery" if is_pending_delivery else "en_cours",
                "pending_message": message if is_pending_delivery else None,
                "pending_message_at": now_iso() if is_pending_delivery else None,
            },
            timeout=10.0,
        )
        res.raise_for_status()

    return {
        "conversation_id": conversation_id,
        "stage": "auto_23h",
        "message": message,
        "sent": sent,
        "status": "pending_delivery" if is_pending_delivery else "sent",
    }


# ── Cron auto 23h check (remplace le trigger ManyChat) ───────────────────────


@app.post("/follow-ups/cron-auto-check")
async def cron_auto_follow_up_check(
    x_dashboard_secret: Optional[str] = Header(default=None),
):
    """Cron endpoint: scan all conversations and send auto 23h follow-ups for due ones.
    Replaces the ManyChat trigger that doesn't fire reliably."""
    require_dashboard_secret(x_dashboard_secret)

    results = {"checked": 0, "auto_sent": 0, "errors": 0, "details": []}

    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATIONS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params={
                "order": "created_at.desc",
                "limit": "500",
                "select": "id,created_at,username,display_name,message,status,agent_active,automation_mode,history,channel,external_contact_id,phone_e164,last_inbound_at",
            },
            timeout=10.0,
        )
        res.raise_for_status()
        conversations = res.json()

    for conv in conversations:
        due_item = build_follow_up_item(conv)
        if not due_item or due_item.get("stage") != "auto_23h":
            continue

        results["auto_sent"] += 1
        try:
            conv_user_id = conv.get("user_id")
            if conv_user_id:
                await enforce_ai_cost_cap(conv_user_id)
            message = await generate_follow_up_message(conv, "auto_23h")
            if conv_user_id:
                await record_ai_usage_event(conv_user_id, "follow_up_cron_auto_23h", json.dumps(conv.get("history") or [], ensure_ascii=False), message)
            send_result = await send_channel_message(conv, message)
            sent = send_result.get("status_code", 500) < 400
            is_pending_delivery = is_manychat_pending_delivery_error(send_result)
            if not sent and not is_pending_delivery:
                raise HTTPException(status_code=502, detail=f"Send error: {send_result.get('body')}")

            history = conv.get("history") or []
            new_history = history + [{
                "role": "assistant",
                "content": message,
                "timestamp": now_iso(),
                "follow_up_stage": "auto_23h",
                "follow_up_mode": "auto",
                "source": "follow_up_cron",
                "sent": sent,
                "pending_delivery": is_pending_delivery,
                "delivery_failed": is_pending_delivery,
                "delivery_status": "pending_delivery" if is_pending_delivery else "sent",
                "send_status_code": send_result.get("status_code"),
                **({"send_error_body": (send_result.get("body") or "")[:500]} if is_pending_delivery else {}),
            }]
            if len(new_history) > MAX_HISTORY_TURNS * 2:
                new_history = new_history[-(MAX_HISTORY_TURNS * 2):]

            async with httpx.AsyncClient() as http2:
                await http2.patch(
                    SUPABASE_CONVERSATIONS_URL,
                    headers={**supabase_headers(), "Prefer": "return=minimal"},
                    params={"id": f"eq.{conv['id']}"},
                    json={
                        "response": message,
                        "history": new_history,
                        "status": "pending_delivery" if is_pending_delivery else "en_cours",
                        "pending_message": message if is_pending_delivery else None,
                        "pending_message_at": now_iso() if is_pending_delivery else None,
                    },
                    timeout=10.0,
                )

            results["details"].append({
                "conversation_id": conv["id"],
                "username": conv.get("username"),
                "sent": sent,
                "status": "pending_delivery" if is_pending_delivery else "sent",
                "send_status": send_result.get("status_code"),
            })
        except Exception as e:
            results["errors"] += 1
            results["details"].append({
                "conversation_id": conv["id"],
                "username": conv.get("username"),
                "sent": False,
                "error": str(e)[:200],
            })

    results["checked"] = len(conversations)
    return results


# ── Playground endpoint ───────────────────────────────────────────────────────

@app.post("/playground")
async def playground(
    payload: PlaygroundPayload,
    user_id: str = Depends(require_jwt),
):

    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")
    system_prompt = build_generation_prompt(await get_active_prompt(user_id))
    if payload.calendly_url or payload.sales_page_url:
        system_prompt = append_agent_options(
            system_prompt,
            calendly_url=(payload.calendly_url or "").strip(),
            sales_page_url=(payload.sales_page_url or "").strip(),
        )
    try:
        reply = generate_claude_reply(payload.messages, system_prompt)
    except ProviderGenerationError as e:
        return provider_error_response(e)
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
        return provider_error_response(classify_provider_error(e))

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


@app.post("/reviews/daily")
async def run_daily_reviews(
    payload: DailyReviewPayload,
    x_dashboard_secret: Optional[str] = Header(default=None),
):
    """Cron-compatible daily review trigger protected by DASHBOARD_SECRET."""
    require_dashboard_secret(x_dashboard_secret)
    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id is required")
    result = await run_daily_conversation_review_job(
        user_id=user_id,
        review_date=payload.review_date,
        limit=payload.limit,
        conversation_id=(payload.conversation_id or None),
    )
    print(
        f"[reviews:daily] user_id={user_id} date={result['review_date']} "
        f"selected={result['selected']} stored={result['stored']} errors={len(result.get('errors', []))}"
    )
    return result


@app.get("/reviews/daily")
async def get_daily_reviews(
    review_date: Optional[str] = Query(default=None),
    lesson_status: Optional[str] = Query(default=None),
    limit: int = Query(default=100),
    user_id: str = Depends(require_jwt),
):
    params = {
        "user_id": f"eq.{user_id}",
        "order": "review_date.desc,created_at.desc",
        "limit": str(min(max(limit, 1), 200)),
        "select": "id,created_at,review_date,conversation_id,username,objective_reached,objective_reason,human_likeness_score,sales_effectiveness_score,engagement_score,moment_of_failure,failure_category,what_angellos_did_wrong,better_human_reply,lesson_learned,prompt_rule_candidate,lesson_status",
    }
    if review_date:
        params["review_date"] = f"eq.{review_date}"
    if lesson_status:
        params["lesson_status"] = f"eq.{lesson_status}"
    async with httpx.AsyncClient() as http:
        res = await http.get(
            SUPABASE_CONVERSATION_REVIEWS_URL,
            headers={**supabase_headers(), "Accept": "application/json"},
            params=params,
            timeout=10.0,
        )
        res.raise_for_status()
        return res.json()


@app.patch("/reviews/{review_id}/lesson-status")
async def update_review_lesson_status(
    review_id: str,
    payload: ReviewLessonStatusPayload,
    user_id: str = Depends(require_jwt),
):
    if payload.lesson_status not in {"candidate", "approved", "rejected", "ignored"}:
        raise HTTPException(status_code=400, detail="Invalid lesson_status")
    patch_body: dict = {"lesson_status": payload.lesson_status}
    if payload.lesson_status == "approved":
        patch_body["approved_at"] = now_iso()
    else:
        patch_body["approved_at"] = None
    async with httpx.AsyncClient() as http:
        res = await http.patch(
            SUPABASE_CONVERSATION_REVIEWS_URL,
            headers={**supabase_headers(), "Prefer": "return=representation"},
            params={"id": f"eq.{review_id}", "user_id": f"eq.{user_id}"},
            json=patch_body,
            timeout=10.0,
        )
        res.raise_for_status()
        rows = res.json()
    if not rows:
        raise HTTPException(status_code=404, detail="Review not found")
    return review_public_payload(rows[0])


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
        f"Language rules:\n"
        f"- Default to English for Angellos English beta.\n"
        f"- Return the modified prompt, diff lines, and justifications in English.\n"
        f"- If the active prompt contains French labels, translate modified/saved labels into English.\n"
        f"- Never output French labels like 'TON RÔLE'; use English labels like 'Your role'.\n"
        f"- Use French only if the user's full business setup is explicitly written in French.\n"
        f'Generate the complete modified prompt, then return ONLY valid JSON:\n'
        f'{{"prompt_proposed": "<complete modified prompt>", "diff": [{{"line": "<text>", "type": "add|remove|keep", "justification": "<why>"}}]}}\n'
        f'In the diff: "add" = added or modified line, "remove" = deleted or replaced line, '
        f'"keep" = unchanged line near a change (context). '
        f'Justification is required only for each add/remove.\n'
        f"Return only JSON, with no text before or after."
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=(
                "You are an expert in AI prompt optimization. "
                "Default language is English for Angellos English beta. "
                "Return generated prompt lines, diffs, summaries, and justifications in English. "
                "Never use French labels like 'TON RÔLE'; use English labels like 'Your role'."
            ),
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()
    except Exception as e:
        return provider_error_response(classify_provider_error(e))

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
            "Default language is English for Angellos English beta. "
            "Return every generated rule, summary, change description, workflow update, target section, and saved instruction in English. "
            "Do not write French unless the user's full business setup is explicitly written in French. "
            "Never output French section labels such as 'TON RÔLE'; use English labels such as 'Your role'. "
            "Return only valid JSON, with no markdown."
        )
        user_message = (
            "User instruction:\n"
            f"{instruction}\n\n"
            "Active prompt:\n"
            f"<active_prompt>\n{current_prompt}\n</active_prompt>\n\n"
            "Language rules:\n"
            "- Angellos is currently in the English beta, so English is the default language.\n"
            "- Keep the updated prompt, generated rules, target_section, summary, and changes in English.\n"
            "- If the active prompt contains French headings or rules, translate the modified/saved version into natural English.\n"
            "- Use labels like 'Your role', 'Qualification process', and 'Orientation flow'. Never use 'TON RÔLE' or other French labels.\n"
            "- Only use French if the user's full business setup is explicitly written in French.\n\n"
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
                model="claude-sonnet-4-6",
                max_tokens=8192,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
            raw = response.content[0].text.strip()
            result = normalize_prompt_refinement_result(parse_llm_json(raw), current_prompt)
            if looks_like_french_refinement_text(result):
                retry_message = (
                    f"{user_message}\n\n"
                    "The previous draft used French. Regenerate the same surgical update fully in English. "
                    "Translate any French rule labels into English. Return only the required JSON."
                )
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=8192,
                    system=system,
                    messages=[{"role": "user", "content": retry_message}],
                )
                raw = response.content[0].text.strip()
                result = normalize_prompt_refinement_result(parse_llm_json(raw), current_prompt)
                if looks_like_french_refinement_text(result):
                    raise ValueError("Claude returned French text for an English beta prompt refinement")
        except ProviderGenerationError as e:
            return provider_error_response(e)
        except Exception as e:
            return provider_error_response(classify_provider_error(e))

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
            try:
                res = await http.post(
                    SUPABASE_PROMPT_VERSIONS_URL,
                    headers={**supabase_headers(), "Prefer": "return=representation"},
                    json=insert_payload,
                    timeout=10.0,
                )
                res.raise_for_status()
            except httpx.HTTPStatusError as insert_error:
                if not is_supabase_schema_cache_error(insert_error):
                    raise
                print(
                    "[refine-prompt:apply:schema-fallback] "
                    f"status={insert_error.response.status_code} body={insert_error.response.text[:1000]}"
                )
                legacy_payload = {
                    **row_owner_fields(user_id),
                    "content": updated_prompt,
                    "is_active": True,
                    "source": prompt_refinement_source(instruction),
                    "insight_id": None,
                }
                res = await http.post(
                    SUPABASE_PROMPT_VERSIONS_URL,
                    headers={**supabase_headers(), "Prefer": "return=representation"},
                    json=legacy_payload,
                    timeout=10.0,
                )
                res.raise_for_status()
            created = res.json()
            new_version = created[0] if isinstance(created, list) else created
    except Exception as e:
        return prompt_refinement_save_error_response(e)

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
            params = {
                "order": "created_at.desc",
                "select": "id,created_at,is_active,source,insight_id,refinement_instruction,refinement_applied_at,previous_version_id",
                **owner_scope(user_id),
            }
            try:
                res = await http.get(
                    SUPABASE_PROMPT_VERSIONS_URL,
                    headers={**supabase_headers(), "Accept": "application/json"},
                    params=params,
                    timeout=10.0,
                )
                res.raise_for_status()
            except httpx.HTTPStatusError as select_error:
                if not is_supabase_schema_cache_error(select_error):
                    raise
                print(
                    "[prompt-versions:schema-fallback] "
                    f"status={select_error.response.status_code} body={select_error.response.text[:1000]}"
                )
                res = await http.get(
                    SUPABASE_PROMPT_VERSIONS_URL,
                    headers={**supabase_headers(), "Accept": "application/json"},
                    params={
                        "order": "created_at.desc",
                        "select": "id,created_at,is_active,source,insight_id",
                        **owner_scope(user_id),
                    },
                    timeout=10.0,
                )
                res.raise_for_status()
            return res.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase error: {e}")


@app.get("/prompt-versions/{version_id}/memory")
async def get_prompt_version_memory(
    version_id: str,
    user_id: str = Depends(require_jwt),
):
    try:
        version = await fetch_prompt_version_for_memory(version_id, user_id)
        previous_version = await fetch_previous_prompt_version_for_memory(version, user_id)
        return prompt_version_memory_snapshot(version, previous_version)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[prompt-version-memory:error] version_id={version_id} error={type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail="Unable to inspect this version")


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
    developer_mode: bool = Query(default=False),
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
        "knowledge_voice": bool(
            (profile or {}).get("profile", {}).get("sales_process")
            or (profile or {}).get("profile", {}).get("voice_profile")
        ),
        "avatar_client": bool(avatar and avatar.get("avatar")),
        "test_conversation": False,
    }
    completed = sum(1 for value in checklist.values() if value)
    response_payload = {
        "profile": profile,
        "avatar": avatar,
        # The standard Training Center UI needs learned rules to render
        # user-facing sections like "Do not say". Developer mode only controls
        # the raw advanced JSON editor below.
        "sales_rules": sales_rules,
        "main_steps": TRAINING_CENTER_MAIN_STEPS,
        "checklist": checklist,
        "progress_score": round((completed / len(checklist)) * 100),
        "what_angellos_knows": {
            "conversation_guidance": summarize_agent_sales_rules(sales_rules),
        },
        "advanced": {
            "developer_mode": developer_mode,
            "conversation_rules_available": bool(sales_rules and sales_rules.get("rules")),
            "conversation_rules": sales_rules if developer_mode else None,
        },
    }
    return response_payload


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
        "Default language is English for Angellos English beta. "
        "Return all summaries, rules, list items, and labels in English unless the user's full business setup is explicitly in French. "
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
    except ProviderGenerationError as e:
        return provider_error_response(e)
    except Exception:
        return provider_error_response(classify_provider_error(Exception("Invalid AI response")))

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
        "Default language is English for Angellos English beta. "
        "Return all generated rules, workflow updates, summaries, and labels in English unless the user's full business setup is explicitly in French. "
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
    except ProviderGenerationError as e:
        return provider_error_response(e)
    except Exception:
        return provider_error_response(classify_provider_error(Exception("Invalid AI response")))

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


@app.post("/agent/knowledge/extract")
async def extract_agent_knowledge(
    payload: KnowledgeExtractPayload,
    user_id: str = Depends(require_jwt),
):
    if client is None:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")

    uploaded_text = extract_text_from_uploaded_knowledge(payload.file_name, payload.file_base64)
    source_text = "\n\n".join(
        part for part in [
            f"Manual sales process:\n{payload.manual_process.strip()}" if payload.manual_process.strip() else "",
            f"Uploaded or pasted knowledge:\n{payload.pasted_text.strip()}" if payload.pasted_text.strip() else "",
            f"Uploaded file text:\n{uploaded_text}" if uploaded_text else "",
        ]
        if part
    ).strip()
    if len(source_text) < 20:
        raise HTTPException(status_code=422, detail="Add a sales process or paste document text first")

    system = (
        "You extract structured sales knowledge and voice profile for Angellos, an Instagram DM setter. "
        "Do not copy long-form transcript wording into DM replies. Learn the user's voice, then adapt it to short, natural Instagram DMs. "
        "Default language is English for Angellos English beta. "
        "Return extracted summaries, generated rules, workflow updates, and preview items in English unless the full source business setup is explicitly in French. "
        "Return only valid JSON with no markdown."
    )
    user_message = (
        f"Source name: {payload.file_name or 'manual input'}\n"
        f"Source type: {payload.file_type or payload.category or 'mixed'}\n\n"
        f"{source_text[:120000]}\n\n"
        "Extract exactly this JSON shape:\n"
        "{\n"
        '  "profile_patch": {\n'
        '    "raw_notes": "",\n'
        '    "sales_process": "",\n'
        '    "next_step": "",\n'
        '    "voice_profile": "",\n'
        '    "tone_rules": [],\n'
        '    "forbidden_phrases": [],\n'
        '    "knowledge_sources": []\n'
        "  },\n"
        '  "avatar_patch": {\n'
        '    "persona_summary": "",\n'
        '    "pain_points": [],\n'
        '    "objections": [],\n'
        '    "buying_triggers": [],\n'
        '    "bad_fit": [],\n'
        '    "exact_words": []\n'
        "  },\n"
        '  "rules_patch": {\n'
        '    "qualification_questions": [],\n'
        '    "buying_signals": [],\n'
        '    "call_offer_conditions": [],\n'
        '    "red_flags": [],\n'
        '    "stop_conditions": [],\n'
        '    "objection_responses": [],\n'
        '    "faq_answers": [],\n'
        '    "follow_up_rules": [],\n'
        '    "do_not_say": [],\n'
        '    "escalation_rules": [],\n'
        '    "links_or_resources": []\n'
        "  },\n"
        '  "preview": {\n'
        '    "sales_process_found": [],\n'
        '    "qualification_questions_found": [],\n'
        '    "good_fit_signals": [],\n'
        '    "bad_fit_signals": [],\n'
        '    "next_step": [],\n'
        '    "objection_answers": [],\n'
        '    "faq_answers": [],\n'
        '    "voice_profile_found": [],\n'
        '    "phrases_to_use": [],\n'
        '    "phrases_to_avoid": []\n'
        "  }\n"
        "}\n"
        "Keep every list item short and editable. If a field is unknown, use an empty string or empty list."
    )
    try:
        raw = generate_claude_reply([{"role": "user", "content": user_message}], system)
        extracted = clean_json_value(parse_llm_json(raw))
    except ProviderGenerationError as e:
        return provider_error_response(e)
    except Exception:
        return provider_error_response(classify_provider_error(Exception("Invalid AI response")))

    return {
        "profile_patch": extracted.get("profile_patch") or {},
        "avatar_patch": extracted.get("avatar_patch") or {},
        "rules_patch": extracted.get("rules_patch") or {},
        "preview": extracted.get("preview") or {},
    }


@app.post("/agent/knowledge/train")
async def train_agent_from_knowledge(
    payload: KnowledgeTrainPayload,
    user_id: str = Depends(require_jwt),
):
    try:
        profile_row = await get_user_singleton_row(SUPABASE_AGENT_PROFILES_URL, user_id)
        avatar_row = await get_user_singleton_row(SUPABASE_AGENT_AVATARS_URL, user_id)
        sales_rules_row = await get_user_singleton_row(SUPABASE_AGENT_SALES_RULES_URL, user_id)

        profile = merge_structured_patch((profile_row or {}).get("profile") or {}, payload.profile_patch)
        avatar = merge_structured_patch((avatar_row or {}).get("avatar") or {}, payload.avatar_patch)
        rules = merge_structured_patch((sales_rules_row or {}).get("rules") or {}, payload.rules_patch)

        profile_saved = await upsert_user_singleton_row(
            SUPABASE_AGENT_PROFILES_URL,
            user_id,
            {"profile": profile},
        )
        avatar_saved = await upsert_user_singleton_row(
            SUPABASE_AGENT_AVATARS_URL,
            user_id,
            {
                "source_inputs": (avatar_row or {}).get("source_inputs") or {},
                "avatar": avatar,
            },
        )
        rules_saved = await upsert_user_singleton_row(
            SUPABASE_AGENT_SALES_RULES_URL,
            user_id,
            {"rules": rules},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Knowledge save error: {e}")

    return {
        "success": True,
        "profile": profile_saved,
        "avatar": avatar_saved,
        "sales_rules": rules_saved,
    }


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
