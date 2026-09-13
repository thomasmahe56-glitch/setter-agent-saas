import asyncio
import json
from pathlib import Path

import main


def test_persistent_reservation_is_tenant_and_channel_scoped(monkeypatch):
    reservations = set()
    patches = []

    class Response:
        status_code = 201

        def __init__(self, payload):
            self.payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self.payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **kwargs):
            row = kwargs["json"]
            key = (row["user_id"], row["channel"], row["event_id"])
            if key in reservations:
                return Response([])
            reservations.add(key)
            return Response([row])

        async def patch(self, _url, **kwargs):
            patches.append(kwargs)
            return Response([])

    monkeypatch.setattr(main.config, "setter_persistent_idempotency_enabled", True)
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)

    async def scenario():
        assert await main.reserve_inbound_event("tenant-a", "instagram", "evt-1") is True
        assert await main.reserve_inbound_event("tenant-a", "instagram", "evt-1") is False
        assert await main.reserve_inbound_event("tenant-b", "instagram", "evt-1") is True
        assert await main.reserve_inbound_event("tenant-a", "whatsapp", "evt-1") is True
        await main.finish_inbound_event(
            "tenant-a", "instagram", "evt-1", status="completed", conversation_id="conversation-a"
        )

    asyncio.run(scenario())
    assert patches[0]["params"] == {
        "user_id": "eq.tenant-a",
        "channel": "eq.instagram",
        "event_id": "eq.evt-1",
    }
    assert patches[0]["json"]["status"] == "completed"


def test_events_without_reliable_id_keep_fast_cache_only(monkeypatch):
    monkeypatch.setattr(main.config, "setter_persistent_idempotency_enabled", True)

    class ExplodingClient:
        async def __aenter__(self):
            raise AssertionError("persistent table must not be used without a reliable provider event ID")

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(main.httpx, "AsyncClient", ExplodingClient)
    assert asyncio.run(main.reserve_inbound_event("tenant-a", "instagram", None)) is True


def test_duplicate_webhook_after_process_cache_loss_invokes_inbound_only_once(monkeypatch):
    reservations = set()
    inbound_calls = 0

    async def fake_require_secret(_secret):
        return "tenant-a"

    async def fake_reserve(user_id, channel, event_id):
        key = (user_id, channel, event_id)
        if key in reservations:
            return False
        reservations.add(key)
        return True

    async def fake_finish(*_args, **_kwargs):
        return None

    async def fake_inbound(**_kwargs):
        nonlocal inbound_calls
        inbound_calls += 1
        return {
            "reply": "Hello", "sent": False, "should_send": True, "mode": "auto",
            "skipped": False, "reason": None, "error": None, "conversation_id": "conversation-a",
        }

    monkeypatch.setattr(main, "require_secret", fake_require_secret)
    monkeypatch.setattr(main, "reserve_inbound_event", fake_reserve)
    monkeypatch.setattr(main, "finish_inbound_event", fake_finish)
    monkeypatch.setattr(main, "handle_inbound_message", fake_inbound)
    payload = main.WebhookPayload(
        username="real_handle", message="Hello", subscriber_id="subscriber-a", event_id="evt-stable-1"
    )

    async def scenario():
        main._webhook_replay_cache.clear()
        first = await main.webhook(payload, x_webhook_secret="secret")
        main._webhook_replay_cache.clear()  # Simulates a restart or another Railway instance.
        second = await main.webhook(payload, x_webhook_secret="secret")
        return first, second

    first, second = asyncio.run(scenario())
    assert inbound_calls == 1
    assert first["agent_response"] == "Hello"
    assert second["reason"] == "duplicate_message"


def test_processed_event_migration_is_tenant_scoped_least_privilege_and_non_destructive():
    sql = (Path(__file__).parent / "migrations" / "add_processed_inbound_events.sql").read_text().lower()
    assert "create extension if not exists pgcrypto" in sql
    assert "create table if not exists public.processed_inbound_events" in sql
    assert "unique (user_id, channel, event_id)" in sql
    assert "create index if not exists processed_inbound_events_user_created_idx" in sql
    assert "enable row level security" in sql
    assert "revoke all on table public.processed_inbound_events from anon, authenticated" in sql
    assert "grant select, insert, update on table public.processed_inbound_events to service_role" in sql
    assert "grant all" not in sql
    assert not any(statement in sql for statement in ("drop ", "truncate ", "delete from"))
