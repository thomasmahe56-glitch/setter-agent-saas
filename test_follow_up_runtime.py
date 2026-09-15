import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import main


NOW = datetime(2026, 9, 15, 15, tzinfo=timezone.utc)
ANCHOR = NOW - timedelta(hours=7)


def job(**changes):
    result = {
        "id": "job-1", "conversation_id": "conversation-1", "user_id": "tenant-1",
        "stage": "follow_up_1", "anchor_at": ANCHOR.isoformat(),
        "due_at": NOW.isoformat(), "scheduled_at": NOW.isoformat(),
        "mode": "auto", "config_version": 1, "status": "processing",
        "idempotency_key": "conversation-1:follow_up_1:1",
    }
    return {**result, **changes}


def conversation(**changes):
    result = {
        "id": "conversation-1", "user_id": "tenant-1", "channel": "instagram",
        "agent_active": True, "automation_mode": "auto", "human_takeover": False,
        "contact_status": "active", "last_inbound_at": (ANCHOR - timedelta(hours=1)).isoformat(),
        "history": [{"role": "assistant", "content": "Hello", "timestamp": ANCHOR.isoformat(), "sent": True}],
    }
    return {**result, **changes}


def settings(**changes):
    result = {
        "follow_up_config_version": 1, "timezone": "Europe/Paris",
        "allowed_send_start": "08:00", "allowed_send_end": "20:00",
        "follow_up_config": [{"enabled": True, "delay_value": 7, "delay_unit": "hours", "mode": "auto"}],
    }
    return {**result, **changes}


def setup(monkeypatch, *, conv=None, config=None):
    monkeypatch.setattr(main, "get_conversation_by_id", AsyncMock(return_value=conv or conversation()))
    monkeypatch.setattr(main, "get_follow_up_settings_strict", AsyncMock(return_value=config or settings()))
    patch = AsyncMock(return_value=True)
    send = AsyncMock(return_value={"status_code": 200, "body": "{}"})
    monkeypatch.setattr(main, "patch_follow_up_job", patch)
    monkeypatch.setattr(main, "send_channel_message", send)
    monkeypatch.setattr(main, "enforce_ai_cost_cap", AsyncMock())
    monkeypatch.setattr(main, "generate_follow_up_result", AsyncMock(return_value=SimpleNamespace(text="A short follow-up")))
    monkeypatch.setattr(main, "record_ai_usage_event", AsyncMock())
    return patch, send


def test_server_api_key_headers_support_new_and_legacy_supabase_keys(monkeypatch):
    monkeypatch.setattr(main, "SUPABASE_SERVICE_KEY", "sb_secret_test_only")
    new_headers = main.supabase_headers()
    assert new_headers["apikey"] == "sb_secret_test_only"
    assert "Authorization" not in new_headers
    monkeypatch.setattr(main, "SUPABASE_SERVICE_KEY", "eyJlegacy-test-only")
    legacy_headers = main.supabase_headers()
    assert legacy_headers["apikey"] == "eyJlegacy-test-only"
    assert legacy_headers["Authorization"] == "Bearer eyJlegacy-test-only"


def test_test_service_can_pause_workers_until_credentials_are_configured(monkeypatch):
    monkeypatch.setenv("BACKEND_WORKERS_ENABLED", "false")
    scheduled = AsyncMock()
    follow_up = AsyncMock()
    monkeypatch.setattr(main, "scheduled_reply_worker", scheduled)
    monkeypatch.setattr(main, "follow_up_worker", follow_up)
    async def run():
        async with main.app_lifespan(main.app):
            pass
    asyncio.run(run())
    scheduled.assert_not_awaited()
    follow_up.assert_not_awaited()


def test_prospect_reply_cancels_old_job_before_send(monkeypatch):
    latest = ANCHOR + timedelta(hours=4)
    patch, send = setup(monkeypatch, conv=conversation(last_inbound_at=latest.isoformat(),
        history=[{"role": "user", "timestamp": latest.isoformat(), "content": "Interested"}]))
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "cancelled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "cancelled"


def test_newer_assistant_message_cancels_superseded_job(monkeypatch):
    latest = ANCHOR + timedelta(hours=2)
    patch, send = setup(monkeypatch, conv=conversation(history=[
        {"role": "assistant", "timestamp": ANCHOR.isoformat(), "sent": True},
        {"role": "assistant", "timestamp": latest.isoformat(), "sent": True}]))
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "cancelled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "cancelled"


def test_booked_conversation_cancels_follow_up(monkeypatch):
    patch, send = setup(monkeypatch, conv=conversation(status="appel_booke"))
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "cancelled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "cancelled"


def test_virtual_clock_honors_custom_delay_before_provider_call(monkeypatch):
    patch, send = setup(monkeypatch)
    assert asyncio.run(main.execute_follow_up_job(job(), now=ANCHOR + timedelta(hours=6, minutes=59))) == "scheduled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "scheduled"


def test_changed_config_cancels_stale_job(monkeypatch):
    patch, send = setup(monkeypatch, config=settings(follow_up_config_version=2))
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "cancelled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "cancelled"


def test_config_change_recalculates_future_job_and_cancels_previous_version(monkeypatch):
    patch, send = setup(monkeypatch, config=settings(follow_up_config_version=2,
        follow_up_config=[{"enabled": True, "delay_value": 12, "delay_unit": "hours", "mode": "auto"}]))
    existing = [{"id": "old-job", "status": "scheduled", "idempotency_key": "old-version-key"}]
    inserted = []
    class Response:
        def __init__(self, data=None): self.data = data or []
        def raise_for_status(self): pass
        def json(self): return self.data
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, *args, **kwargs): return Response(existing)
        async def post(self, *args, **kwargs):
            inserted.extend(kwargs["json"])
            return Response()
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    asyncio.run(main.reconcile_follow_up_conversation("conversation-1", "tenant-1"))
    send.assert_not_awaited()
    assert patch.await_args.args[0] == "old-job"
    assert patch.await_args.args[1]["status"] == "cancelled"
    assert len(inserted) == 1
    assert inserted[0]["due_at"] == (ANCHOR + timedelta(hours=12)).isoformat()
    assert inserted[0]["config_version"] == 2


def test_prospect_reply_reconciliation_cancels_unsent_job(monkeypatch):
    reply_at = ANCHOR + timedelta(hours=2)
    patch, send = setup(monkeypatch, conv=conversation(last_inbound_at=reply_at.isoformat(),
        history=[{"role": "assistant", "timestamp": ANCHOR.isoformat(), "sent": True},
                 {"role": "user", "timestamp": reply_at.isoformat(), "content": "Hi"}]))
    class Response:
        def raise_for_status(self): pass
        def json(self): return [{"id": "old-job", "status": "scheduled", "idempotency_key": "old-key"}]
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, *args, **kwargs): return Response()
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    asyncio.run(main.reconcile_follow_up_conversation("conversation-1", "tenant-1"))
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "cancelled"


def test_meta_window_expired_requires_manual_action(monkeypatch):
    patch, send = setup(monkeypatch, conv=conversation(last_inbound_at=(NOW - timedelta(days=3)).isoformat()))
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "manual_required"
    send.assert_not_awaited()
    assert "window closed" in patch.await_args.args[1]["last_error"]


def test_closed_hours_requeue_without_early_send(monkeypatch):
    patch, send = setup(monkeypatch)
    closed_time = datetime(2026, 9, 15, 22, tzinfo=timezone.utc)
    assert asyncio.run(main.execute_follow_up_job(job(), now=closed_time)) == "scheduled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["scheduled_at"] == datetime(2026, 9, 16, 6, tzinfo=timezone.utc).isoformat()


def test_ambiguous_provider_timeout_is_never_retried_automatically(monkeypatch):
    patch, send = setup(monkeypatch)
    send.side_effect = TimeoutError()
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "manual_required"
    assert send.await_count == 1
    assert patch.await_args.args[1]["status"] == "manual_required"


def test_ai_preparation_error_is_failed_without_provider_attempt(monkeypatch):
    patch, send = setup(monkeypatch)
    main.generate_follow_up_result.side_effect = RuntimeError("No model")
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "failed"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "failed"


def test_worker_loop_is_server_side_and_independent_of_dashboard(monkeypatch):
    refresh = AsyncMock(return_value=1)
    due = AsyncMock(return_value=1)
    monkeypatch.setattr(main, "process_follow_up_refresh_queue", refresh)
    monkeypatch.setattr(main, "process_due_follow_up_jobs", due)
    async def stop_after_one_tick(_):
        raise asyncio.CancelledError()
    monkeypatch.setattr(main.asyncio, "sleep", stop_after_one_tick)
    try:
        asyncio.run(main.follow_up_worker())
    except asyncio.CancelledError:
        pass
    refresh.assert_awaited_once()
    due.assert_awaited_once()


def test_persisted_claim_is_processed_once_after_worker_reinstantiation(monkeypatch):
    stored = job(status="scheduled")  # The row remains in the fake database, not in a browser or worker instance.
    execute = AsyncMock(return_value="sent")
    monkeypatch.setattr(main, "execute_follow_up_job", execute)
    class Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, *args, **kwargs):
            if url.endswith("claim_due_follow_up_jobs"):
                if stored["status"] != "scheduled": return Response([])
                stored["status"] = "processing"
                return Response([dict(stored)])
            return Response(0)
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    async def run_two_instances():
        first = await main.process_due_follow_up_jobs()
        stored["status"] = "sent"
        second = await main.process_due_follow_up_jobs()
        return first, second
    assert asyncio.run(run_two_instances()) == (1, 0)
    execute.assert_awaited_once()


def test_two_workers_cannot_receive_the_same_claimed_job(monkeypatch):
    stored = job(status="scheduled")
    execute = AsyncMock(return_value="sent")
    monkeypatch.setattr(main, "execute_follow_up_job", execute)
    class Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, *args, **kwargs):
            if url.endswith("claim_due_follow_up_jobs"):
                if stored["status"] != "scheduled": return Response([])
                stored["status"] = "processing"
                return Response([dict(stored)])
            return Response(0)
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    async def run_both():
        return await asyncio.gather(main.process_due_follow_up_jobs(), main.process_due_follow_up_jobs())
    assert sorted(asyncio.run(run_both())) == [0, 1]
    execute.assert_awaited_once()
