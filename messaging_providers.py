"""Provider-neutral messaging primitives for Angellos.

The Setter core consumes ``NormalizedInboundMessage`` and never needs to know
the provider-specific webhook shape. Provider credentials stay server-side.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


META_PROVIDER = "meta_instagram"
MANYCHAT_PROVIDER = "manychat"
REQUIRED_META_SCOPES = {"instagram_business_basic", "instagram_business_manage_messages"}


@dataclass(frozen=True)
class NormalizedInboundMessage:
    provider: str
    account_id: str
    external_event_id: str
    external_message_id: str
    sender_id: str
    text: str
    timestamp: datetime
    external_conversation_id: Optional[str] = None
    attachments: list[dict[str, Any]] = field(default_factory=list)


class MessagingProvider(Protocol):
    async def send_message(self, *, account_id: str, recipient_id: str, text: str, access_token: str) -> dict: ...


class ManyChatProvider:
    def __init__(self, api_key: str, *, send_url: str = "https://api.manychat.com/fb/sending/sendContent"):
        self.api_key = api_key
        self.send_url = send_url

    async def send_message(self, *, account_id: str = "", recipient_id: str, text: str, access_token: str = "") -> dict:
        async with httpx.AsyncClient() as http:
            response = await http.post(
                self.send_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "subscriber_id": recipient_id,
                    "data": {
                        "version": "v2",
                        "content": {"messages": [{"type": "text", "text": text}]},
                    },
                },
                timeout=15.0,
            )
        return {"status_code": response.status_code, "body": response.text}


def _encryption_key(raw_key: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(raw_key.encode("ascii"))
    except Exception as exc:
        raise ValueError("MESSAGING_TOKEN_ENCRYPTION_KEY must be URL-safe base64") from exc
    if len(key) != 32:
        raise ValueError("MESSAGING_TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def encrypt_secret(value: str, raw_key: str, *, key_version: str = "v1") -> str:
    """Encrypt a provider secret with AES-256-GCM and a versioned envelope."""
    if not value:
        raise ValueError("Cannot encrypt an empty secret")
    nonce = os.urandom(12)
    ciphertext = AESGCM(_encryption_key(raw_key)).encrypt(nonce, value.encode("utf-8"), key_version.encode())
    return f"{key_version}:{base64.urlsafe_b64encode(nonce + ciphertext).decode('ascii')}"


def decrypt_secret(envelope: str, raw_key: str) -> str:
    key_version, separator, encoded = (envelope or "").partition(":")
    if not separator or key_version != "v1":
        raise ValueError("Unsupported encrypted secret envelope")
    packed = base64.urlsafe_b64decode(encoded.encode("ascii"))
    if len(packed) < 29:
        raise ValueError("Invalid encrypted secret envelope")
    plaintext = AESGCM(_encryption_key(raw_key)).decrypt(packed[:12], packed[12:], key_version.encode())
    return plaintext.decode("utf-8")


def normalize_meta_attachments(message: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for attachment in message.get("attachments") or []:
        payload = attachment.get("payload") or {}
        raw_type = str(attachment.get("type") or "unsupported").lower()
        attachment_type = {
            "image": "image",
            "share": "shared_post_or_reel",
            "story_mention": "story_mention",
            "story_reply": "story_reply",
        }.get(raw_type, "unsupported")
        normalized.append({
            "type": attachment_type,
            "provider_type": raw_type,
            "url": payload.get("url"),
            "title": attachment.get("title"),
        })
    return normalized


def parse_meta_instagram_webhook(payload: dict[str, Any]) -> list[NormalizedInboundMessage]:
    """Parse Meta's Instagram messaging envelope, ignoring delivery echoes."""
    if payload.get("object") not in {"instagram", "page"}:
        return []
    events: list[NormalizedInboundMessage] = []
    for entry in payload.get("entry") or []:
        account_id = str(entry.get("id") or "").strip()
        for event in entry.get("messaging") or []:
            message = event.get("message") or {}
            if message.get("is_echo"):
                continue
            sender_id = str((event.get("sender") or {}).get("id") or "").strip()
            message_id = str(message.get("mid") or "").strip()
            text = str(message.get("text") or "").strip()
            attachments = normalize_meta_attachments(message)
            if not account_id or not sender_id or not message_id or (not text and not attachments):
                continue
            try:
                timestamp = datetime.fromtimestamp(int(event.get("timestamp") or 0) / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                timestamp = datetime.now(timezone.utc)
            if not text:
                kinds = ", ".join(item["type"] for item in attachments)
                text = f"[Instagram attachment: {kinds}]"
            events.append(NormalizedInboundMessage(
                provider=META_PROVIDER,
                account_id=account_id,
                external_event_id=message_id,
                external_message_id=message_id,
                external_conversation_id=str(event.get("conversation_id") or "").strip() or None,
                sender_id=sender_id,
                text=text[:4000],
                timestamp=timestamp,
                attachments=attachments,
            ))
    return events


def can_send_meta_message(
    *,
    connection: dict[str, Any],
    conversation: dict[str, Any],
    now: Optional[datetime] = None,
    reply_window_hours: int = 24,
) -> tuple[bool, str]:
    """Deterministic Meta send gate; the LLM cannot override this decision."""
    if connection.get("status") != "connected":
        return False, "connection_not_active"
    scopes = set(connection.get("scopes") or [])
    if not REQUIRED_META_SCOPES.issubset(scopes):
        return False, "missing_permission"
    if conversation.get("contact_status") == "opted_out" or conversation.get("opted_out_at"):
        return False, "recipient_opted_out"
    if conversation.get("human_takeover"):
        return False, "human_takeover"
    if conversation.get("automation_mode") == "disabled" or not conversation.get("agent_active", True):
        return False, "automation_disabled"
    last_interaction = conversation.get("last_inbound_at")
    if not last_interaction:
        return False, "no_user_initiated_conversation"
    if isinstance(last_interaction, str):
        try:
            last_interaction = datetime.fromisoformat(last_interaction.replace("Z", "+00:00"))
        except ValueError:
            return False, "invalid_last_interaction"
    current = now or datetime.now(timezone.utc)
    if last_interaction.tzinfo is None:
        last_interaction = last_interaction.replace(tzinfo=timezone.utc)
    if current - last_interaction > timedelta(hours=reply_window_hours):
        return False, "outside_standard_messaging_window"
    return True, "eligible"


class MetaInstagramProvider:
    def __init__(self, graph_api_version: str, *, base_url: str = "https://graph.instagram.com"):
        self.graph_api_version = graph_api_version
        self.base_url = base_url.rstrip("/")

    async def send_message(self, *, account_id: str, recipient_id: str, text: str, access_token: str, max_attempts: int = 3) -> dict:
        url = f"{self.base_url}/{self.graph_api_version}/{account_id}/messages"
        retry_after: Optional[float] = None
        for attempt in range(max_attempts):
            try:
                async with httpx.AsyncClient() as http:
                    response = await http.post(
                        url,
                        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                        json={"recipient": {"id": recipient_id}, "message": {"text": text}},
                        timeout=15.0,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == max_attempts - 1:
                    return {"status_code": 503, "body": json.dumps({"error": "network_error", "detail": type(exc).__name__})}
                await asyncio.sleep((2 ** attempt) + random.random())
                continue
            if response.status_code < 400:
                return {"status_code": response.status_code, "body": response.text}
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == max_attempts - 1:
                    return {"status_code": response.status_code, "body": response.text, "retry_after": retry_after}
                header = response.headers.get("retry-after")
                try:
                    retry_after = min(float(header), 30.0) if header else None
                except ValueError:
                    retry_after = None
                await asyncio.sleep(retry_after if retry_after is not None else (2 ** attempt) + random.random())
                continue
            return {"status_code": response.status_code, "body": response.text}
        return {"status_code": 503, "body": '{"error":"provider_unavailable"}'}


EXPLICIT_OPT_OUT = {
    "stop", "unsubscribe", "don't contact me", "do not contact me", "leave me alone",
    "arrête", "arrete", "ne me contacte plus", "ne m'écris plus", "ne m’ecris plus",
}


def is_explicit_opt_out(text: str) -> bool:
    normalized = " ".join((text or "").lower().strip().split())
    return any(phrase == normalized or phrase in normalized for phrase in EXPLICIT_OPT_OUT)
