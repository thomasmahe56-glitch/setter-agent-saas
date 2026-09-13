import asyncio
from pathlib import Path

import main
from conversation_context import (
    build_generation_messages,
    normalize_conversation_memory,
    should_refresh_memory,
)


def _history(count):
    return [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"message-{index}", "timestamp": "ignored"}
        for index in range(count)
    ]


def test_short_conversation_uses_direct_history(monkeypatch):
    monkeypatch.setattr(main.config, "setter_context_compression_enabled", True)
    history = _history(8)
    context = main.setter_generation_context(history, {})
    assert [item["content"] for item in context] == [f"message-{index}" for index in range(8)]
    assert all("timestamp" not in item for item in context)


def test_long_conversation_uses_memory_and_recent_messages_without_mutating_history(monkeypatch):
    monkeypatch.setattr(main.config, "setter_context_compression_enabled", True)
    history = _history(50)
    memory = normalize_conversation_memory({"needs": ["more qualified calls"], "current_stage": "qualification"})
    context = build_generation_messages(history, memory, recent_window=10, metadata_stripper=main.strip_message_metadata)
    assert len(context) == 11
    assert "conversation_memory" in context[0]["content"]
    assert context[1]["content"] == "message-40"
    assert context[-1]["content"] == "message-49"
    assert len(history) == 50


def test_memory_refresh_policy_is_interval_based():
    assert should_refresh_memory(23, 0, threshold_messages=24, refresh_messages=12) is False
    assert should_refresh_memory(24, 0, threshold_messages=24, refresh_messages=12) is True
    assert should_refresh_memory(30, 14, threshold_messages=24, refresh_messages=12, recent_window=10) is False
    assert should_refresh_memory(36, 14, threshold_messages=24, refresh_messages=12, recent_window=10) is True


def test_structured_memory_preserves_sales_critical_information_and_bounds_size():
    memory = normalize_conversation_memory({
        "prospect_context": "Agency owner with a five-person sales team",
        "needs": ["more qualified calls"],
        "pain_points": ["manual follow-up"],
        "qualification": {"budget": "confirmed", "timeline": "this month"},
        "objections": ["worried about setup time"],
        "information_already_given": ["30-day beta", "ManyChat connection"],
        "commitments": ["will review calendar Friday"],
        "current_stage": "call proposed",
        "next_step": "send booking link after confirmation",
        "ignored_freeform_field": "must not survive",
    })
    assert memory["qualification"]["budget"] == "confirmed"
    assert memory["objections"] == ["worried about setup time"]
    assert memory["commitments"] == ["will review calendar Friday"]
    assert memory["current_stage"] == "call proposed"
    assert memory["next_step"] == "send booking link after confirmation"
    assert "ignored_freeform_field" not in memory


def test_inbound_canned_reply_preserves_full_database_history(monkeypatch):
    history = _history(90)
    captured_patches = []

    async def fake_contact(*_args, **_kwargs):
        return {
            "id": "conversation-a",
            "user_id": "tenant-a",
            "history": history,
            "automation_mode": "supervised",
            "agent_active": True,
            "channel": "instagram",
            "external_contact_id": "prospect-a",
            "display_name": "prospect-a",
        }

    async def fake_prompt(_user_id):
        return "prompt"

    async def fake_flush(*_args, **_kwargs):
        return {"attempted": False}

    class Response:
        status_code = 204
        text = ""

        def raise_for_status(self):
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def patch(self, _url, **kwargs):
            captured_patches.append(kwargs["json"])
            return Response()

    monkeypatch.setattr(main, "get_contact_by_external_id", fake_contact)
    monkeypatch.setattr(main, "get_active_prompt", fake_prompt)
    monkeypatch.setattr(main, "flush_pending_deliveries", fake_flush)
    monkeypatch.setattr(main, "is_angellos_acquisition_prompt", lambda _prompt: True)
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)

    result = asyncio.run(main.handle_inbound_message(
        channel="instagram",
        external_contact_id="prospect-a",
        display_name="prospect-a",
        message="no thanks",
        user_id="tenant-a",
    ))
    assert result["skipped"] is False
    persisted_history = captured_patches[-1]["history"]
    assert len(persisted_history) == 92
    assert persisted_history[0]["content"] == "message-0"


def test_conversation_memory_migration_is_idempotent_and_non_destructive():
    sql = (Path(__file__).parent / "migrations" / "add_conversation_memory.sql").read_text().lower()
    assert sql.count("add column if not exists") == 3
    assert "conversation_memory jsonb not null default '{}'::jsonb" in sql
    assert "conversation_memory_updated_at timestamptz" in sql
    assert "conversation_memory_through_message_count integer not null default 0" in sql
    assert "check (conversation_memory_through_message_count >= 0)" in sql
    assert not any(statement in sql for statement in ("drop ", "truncate ", "delete from"))
