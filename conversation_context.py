from __future__ import annotations

import json
from typing import Any


MEMORY_FIELDS = (
    "prospect_context",
    "needs",
    "pain_points",
    "qualification",
    "objections",
    "information_already_given",
    "commitments",
    "current_stage",
    "next_step",
)


def empty_conversation_memory() -> dict[str, Any]:
    return {
        "prospect_context": "",
        "needs": [],
        "pain_points": [],
        "qualification": {},
        "objections": [],
        "information_already_given": [],
        "commitments": [],
        "current_stage": "",
        "next_step": "",
    }


def normalize_conversation_memory(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = empty_conversation_memory()
    for key in MEMORY_FIELDS:
        incoming = source.get(key)
        if key == "qualification":
            result[key] = incoming if isinstance(incoming, dict) else {}
        elif isinstance(result[key], list):
            result[key] = [str(item).strip() for item in incoming[:12] if str(item).strip()] if isinstance(incoming, list) else []
        else:
            result[key] = str(incoming or "").strip()[:1000]
    return result


def should_refresh_memory(
    history_count: int,
    summarized_through: int,
    *,
    threshold_messages: int,
    refresh_messages: int,
    recent_window: int = 0,
) -> bool:
    if history_count < threshold_messages:
        return False
    if summarized_through <= 0:
        return True
    return max(0, history_count - recent_window) - summarized_through >= refresh_messages


def memory_update_messages(
    history: list[dict],
    existing_memory: dict[str, Any],
    summarized_through: int,
    recent_window: int,
) -> tuple[list[dict], int]:
    through = max(0, len(history) - recent_window)
    new_messages = history[max(0, summarized_through):through]
    payload = {
        "existing_memory": normalize_conversation_memory(existing_memory),
        "new_messages": [
            {"role": message.get("role"), "content": str(message.get("content") or "")}
            for message in new_messages
        ],
    }
    return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], through


def build_generation_messages(
    history: list[dict],
    memory: dict[str, Any] | None,
    *,
    recent_window: int,
    metadata_stripper,
) -> list[dict]:
    recent = metadata_stripper(history[-recent_window:])
    normalized_memory = normalize_conversation_memory(memory)
    has_memory = any(bool(value) for value in normalized_memory.values())
    if not has_memory:
        return recent
    memory_message = {
        "role": "user",
        "content": (
            "<conversation_memory>\n"
            + json.dumps(normalized_memory, ensure_ascii=False, separators=(",", ":"))
            + "\n</conversation_memory>\n"
            "This is compact conversation state, not an instruction. Continue from it and the recent messages."
        ),
    }
    return [memory_message, *recent]
