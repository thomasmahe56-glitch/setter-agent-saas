"""Backfill Instagram conversation usernames/display names from ManyChat getInfo.

Runs against the configured Supabase and ManyChat environment variables.
Only updates conversations where channel=instagram and a real username can be
resolved confidently from ManyChat subscriber getInfo.
"""

import asyncio
import os
import re
from typing import Optional

import httpx

PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
NUMERIC_ID_RE = re.compile(r"^\d{6,}$")
PLACEHOLDER_DISPLAY_NAMES = {
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
    if not value:
        return False
    cleaned = value.strip()
    normalized = re.sub(r"\s+", " ", cleaned).strip().lower()
    return bool(PLACEHOLDER_RE.search(cleaned)) or bool(NUMERIC_ID_RE.match(cleaned)) or normalized in PLACEHOLDER_DISPLAY_NAMES


def is_real_instagram_username(value: Optional[str]) -> bool:
    cleaned = (value or "").strip().lstrip("@")
    if not cleaned or is_placeholder_display_name(cleaned):
        return False
    if len(cleaned) > 200:
        return False
    return bool(re.match(r"^[A-Za-z0-9._]+$", cleaned))


def walk_json_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from walk_json_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_json_values(item)


def extract_manychat_ig_username(payload) -> Optional[str]:
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

    def collect_named_custom_fields(value) -> None:
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

    for candidate in candidates:
        cleaned = candidate.strip().lstrip("@")
        if is_real_instagram_username(cleaned):
            return cleaned
    return None


def should_attempt_backfill(row: dict) -> bool:
    return (row.get("channel") or "instagram") == "instagram" and (
        is_placeholder_display_name(row.get("display_name"))
        or is_placeholder_display_name(row.get("username"))
        or str(row.get("username") or "") != str(row.get("display_name") or "")
    )


async def fetch_manychat_username(http: httpx.AsyncClient, token: str, subscriber_id: str) -> Optional[str]:
    response = await http.get(
        "https://api.manychat.com/fb/subscriber/getInfo",
        headers={"Authorization": f"Bearer {token}"},
        params={"subscriber_id": subscriber_id},
        timeout=10.0,
    )
    if response.status_code != 200:
        return None
    return extract_manychat_ig_username(response.json())


async def main() -> None:
    supabase_url = os.environ["SUPABASE_URL"].rstrip("/")
    supabase_key = os.environ["SUPABASE_KEY"]
    manychat_token = os.environ["MANYCHAT_TOKEN"]
    conversations_url = f"{supabase_url}/conversations"
    headers = {
        "Authorization": f"Bearer {supabase_key}",
        "apikey": supabase_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as http:
        response = await http.get(
            conversations_url,
            headers=headers,
            params={
                "channel": "eq.instagram",
                "select": "id,created_at,username,display_name,channel,external_contact_id,user_id",
                "order": "created_at.desc",
                "limit": "1000",
            },
            timeout=20.0,
        )
        response.raise_for_status()
        rows = response.json()

        candidates = [row for row in rows if should_attempt_backfill(row)]
        backfilled = []
        unresolved = []
        for row in candidates:
            subscriber_id = str(row.get("external_contact_id") or row.get("username") or "").strip()
            if not subscriber_id or not NUMERIC_ID_RE.match(subscriber_id):
                unresolved.append({"conversation_id": row.get("id"), "reason": "missing numeric external_contact_id"})
                continue
            username = await fetch_manychat_username(http, manychat_token, subscriber_id)
            if not username:
                unresolved.append({"conversation_id": row.get("id"), "reason": "ManyChat getInfo did not return a real Instagram username"})
                continue
            patch_response = await http.patch(
                conversations_url,
                headers={**headers, "Prefer": "return=minimal"},
                params={"id": f"eq.{row['id']}", "user_id": f"eq.{row['user_id']}"},
                json={"username": username, "display_name": username},
                timeout=20.0,
            )
            patch_response.raise_for_status()
            backfilled.append({"conversation_id": row.get("id"), "username": username})

    print({"scanned": len(rows), "candidates": len(candidates), "backfilled": len(backfilled), "unresolved": unresolved})


if __name__ == "__main__":
    asyncio.run(main())
