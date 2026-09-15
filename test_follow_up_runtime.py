import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

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


def test_successful_provider_response_survives_usage_ledger_failure(monkeypatch, capsys):
    provider = AsyncMock(return_value={"status_code": 200, "body": '{"message_id":"accepted-1"}'})
    monkeypatch.setattr(main, "send_manychat_message", provider)
    monkeypatch.setattr(main, "record_usage_ledger_event", AsyncMock(side_effect=RuntimeError("ledger offline")))
    result = asyncio.run(main.send_channel_message({
        "id": "conversation-1", "user_id": "tenant-1", "channel": "instagram",
        "messaging_provider": main.MANYCHAT_PROVIDER, "external_contact_id": "synthetic-contact",
    }, "A short follow-up", at_most_once=True))
    assert result["status_code"] == 200
    provider.assert_awaited_once()
    assert "sent_but_ledger_failed" in capsys.readouterr().out


@pytest.mark.parametrize("body,expected_status", [
    ('{"status":"success","data":{}}', 200),
    ('{"status":"error","code":3011,"message":"outside window"}', 400),
    ('{"error":{"code":3011}}', 400),
])
def test_manychat_body_status_controls_delivery_and_usage(monkeypatch, body, expected_status):
    provider = AsyncMock(return_value={"status_code": 200, "body": body})
    usage = AsyncMock()
    monkeypatch.setattr(main.MANYCHAT_PROVIDER_CLIENT, "send_message", provider)
    monkeypatch.setattr(main, "record_usage_ledger_event", usage)
    result = asyncio.run(main.send_channel_message({
        "id": "conversation-1", "user_id": "tenant-1", "channel": "instagram",
        "messaging_provider": main.MANYCHAT_PROVIDER, "external_contact_id": "synthetic-contact",
    }, "A short follow-up", at_most_once=True))
    assert result["status_code"] == expected_status
    provider.assert_awaited_once()
    if expected_status == 200:
        usage.assert_awaited_once()
    else:
        usage.assert_not_awaited()
        assert main.is_manychat_pending_delivery_error(result)


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


def test_legacy_dashboard_due_request_stays_loadable_during_backend_first_rollout(monkeypatch):
    monkeypatch.setenv("BACKEND_WORKERS_ENABLED", "false")
    main.app.dependency_overrides[main.require_jwt] = lambda: "tenant-1"
    try:
        with TestClient(main.app) as client:
            response = client.get("/follow-ups/due", params={
                "auto_hours": 23, "manual1_days": 3,
                "manual2_days": 10, "manual3_days": 30,
            })
        assert response.status_code == 200
        assert response.json() == []
    finally:
        main.app.dependency_overrides.pop(main.require_jwt, None)


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


@pytest.mark.parametrize("attempt_count", [1, 2])
def test_due_job_sends_once_and_records_successful_delivery(monkeypatch, attempt_count):
    patch, send = setup(monkeypatch)
    conversation_updates = []

    class Response:
        def raise_for_status(self): pass
        def json(self): return [{"id": "conversation-1"}]

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def patch(self, url, *, json, **kwargs):
            assert url == main.SUPABASE_CONVERSATIONS_URL
            conversation_updates.append(json)
            return Response()

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)

    assert asyncio.run(main.execute_follow_up_job(job(attempt_count=attempt_count), now=NOW)) == "sent"
    send.assert_awaited_once()
    assert send.await_args.kwargs["at_most_once"] is True
    assert patch.await_args.args[1]["status"] == "sent"
    assert patch.await_args.args[1]["sent_at"]
    assert len(conversation_updates) == 1
    delivered = conversation_updates[0]["history"][-1]
    assert delivered["follow_up_job_id"] == "job-1"
    assert delivered["content"] == "A short follow-up"
    assert delivered["sent"] is True
    assert main.record_ai_usage_event.await_args.kwargs["idempotency_key"] == (
        f"conversation-1:follow_up_1:1:ai:{attempt_count}"
    )


def test_reply_added_to_history_during_generation_cancels_before_provider(monkeypatch):
    patch, send = setup(monkeypatch)
    fresh = conversation(history=conversation()["history"] + [
        {"role": "user", "content": "Stop", "timestamp": (ANCHOR + timedelta(hours=1)).isoformat()}
    ])
    main.get_conversation_by_id.side_effect = [conversation(), fresh]
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "cancelled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "cancelled"


def test_takeover_during_generation_cancels_before_provider(monkeypatch):
    patch, send = setup(monkeypatch)
    main.get_conversation_by_id.side_effect = [conversation(), conversation(human_takeover=True)]
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "cancelled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "cancelled"


def test_settings_change_during_generation_cancels_before_provider(monkeypatch):
    patch, send = setup(monkeypatch)
    main.get_follow_up_settings_strict.side_effect = [settings(), settings(follow_up_config_version=2)]
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "cancelled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "cancelled"


def test_closed_hours_during_generation_defer_before_provider(monkeypatch):
    patch, send = setup(monkeypatch)
    next_open = NOW + timedelta(hours=15)
    window = Mock(side_effect=[NOW, next_open])
    monkeypatch.setattr(main, "next_allowed_send_at", window)
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "scheduled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["scheduled_at"] == next_open.isoformat()


def test_meta_window_expiring_during_generation_requires_manual_action(monkeypatch):
    inbound = NOW - timedelta(hours=23, minutes=59)
    patch, send = setup(monkeypatch, conv=conversation(last_inbound_at=inbound.isoformat()))

    class AdvancingClock(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return NOW if cls.calls == 1 else NOW + timedelta(minutes=2)

    monkeypatch.setattr(main, "datetime", AdvancingClock)
    assert asyncio.run(main.execute_follow_up_job(job())) == "manual_required"
    send.assert_not_awaited()
    assert "during preparation" in patch.await_args.args[1]["last_error"]


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


@pytest.mark.parametrize("first_mode", ["auto", "manual"])
def test_only_next_enabled_stage_is_planned_even_when_later_delay_is_shorter(monkeypatch, first_mode):
    config = settings(follow_up_config=[
        {"enabled": True, "delay_value": 12, "delay_unit": "hours", "mode": first_mode},
        {"enabled": True, "delay_value": 7, "delay_unit": "hours", "mode": "auto"},
    ])
    patch, send = setup(monkeypatch, config=config)
    inserted = []

    class Response:
        def raise_for_status(self): pass
        def json(self): return []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, *args, **kwargs): return Response()
        async def post(self, *args, **kwargs):
            inserted.extend(kwargs["json"])
            return Response()

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    asyncio.run(main.reconcile_follow_up_conversation("conversation-1", "tenant-1"))
    assert len(inserted) == 1
    assert inserted[0]["stage"] == "follow_up_1"
    assert inserted[0]["mode"] == first_mode
    assert inserted[0]["due_at"] == (ANCHOR + timedelta(hours=12)).isoformat()
    send.assert_not_awaited()

    # A higher-stage job created by an older backend version is rejected even
    # if it has already been atomically claimed before reconciliation.
    old_stage_two = job(stage="follow_up_2", due_at=NOW.isoformat(), scheduled_at=NOW.isoformat())
    assert asyncio.run(main.execute_follow_up_job(old_stage_two, now=NOW)) == "cancelled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "cancelled"


def test_sent_first_stage_advances_planning_to_second_stage(monkeypatch):
    config = settings(follow_up_config=[
        {"enabled": True, "delay_value": 12, "delay_unit": "hours", "mode": "auto"},
        {"enabled": True, "delay_value": 7, "delay_unit": "hours", "mode": "auto"},
    ])
    conv = conversation(history=[
        {"role": "assistant", "timestamp": (ANCHOR - timedelta(hours=12)).isoformat(), "sent": True},
        {"role": "assistant", "timestamp": ANCHOR.isoformat(), "sent": True,
         "follow_up_stage": "follow_up_1"},
    ])
    _, send = setup(monkeypatch, conv=conv, config=config)
    inserted = []

    class Response:
        def raise_for_status(self): pass
        def json(self): return []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, *args, **kwargs): return Response()
        async def post(self, *args, **kwargs):
            inserted.extend(kwargs["json"])
            return Response()

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    asyncio.run(main.reconcile_follow_up_conversation("conversation-1", "tenant-1"))
    assert len(inserted) == 1
    assert inserted[0]["stage"] == "follow_up_2"
    assert inserted[0]["due_at"] == (ANCHOR + timedelta(hours=7)).isoformat()
    send.assert_not_awaited()


def test_supervised_conversation_uses_manual_job_then_recalculates_on_auto_switch(monkeypatch):
    conv = conversation(automation_mode="supervised")
    patch, send = setup(monkeypatch, conv=conv)
    existing = []
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
    assert len(inserted) == 1
    assert inserted[0]["mode"] == "manual"
    send.assert_not_awaited()

    existing.append({"id": "manual-job", "status": "scheduled",
        "idempotency_key": inserted[0]["idempotency_key"]})
    conv["automation_mode"] = "auto"
    asyncio.run(main.reconcile_follow_up_conversation("conversation-1", "tenant-1"))
    assert patch.await_args.args[0] == "manual-job"
    assert patch.await_args.args[1]["status"] == "cancelled"
    assert len(inserted) == 2
    assert inserted[1]["mode"] == "auto"
    assert inserted[1]["idempotency_key"] != inserted[0]["idempotency_key"]


def test_future_job_outside_meta_window_is_manual_from_planning(monkeypatch):
    old_inbound = ANCHOR - timedelta(days=2)
    conv = conversation(last_inbound_at=old_inbound.isoformat())
    patch, send = setup(monkeypatch, conv=conv)
    inserted = []

    class Response:
        def raise_for_status(self): pass
        def json(self): return []

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, *args, **kwargs): return Response()
        async def post(self, *args, **kwargs):
            inserted.extend(kwargs["json"])
            return Response()

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    asyncio.run(main.reconcile_follow_up_conversation("conversation-1", "tenant-1"))
    assert len(inserted) == 1
    assert inserted[0]["due_at"] == NOW.isoformat()
    assert inserted[0]["scheduled_at"] == NOW.isoformat()
    assert inserted[0]["mode"] == "manual"
    assert "Meta automatic messaging window closed" in inserted[0]["last_error"]

    due_job = job(mode=inserted[0]["mode"], last_error=inserted[0]["last_error"])
    assert asyncio.run(main.execute_follow_up_job(due_job, now=NOW)) == "manual_required"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["last_error"] == inserted[0]["last_error"]


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


def test_provider_window_error_never_marks_follow_up_sent(monkeypatch):
    patch, send = setup(monkeypatch)
    send.return_value = {"status_code": 400,
        "body": '{"status":"error","code":3011,"message":"outside window"}'}
    assert asyncio.run(main.execute_follow_up_job(job(), now=NOW)) == "manual_required"
    send.assert_awaited_once()
    assert patch.await_args.args[1]["status"] == "manual_required"
    assert "sent_at" not in patch.await_args.args[1]


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


def test_ai_preparation_error_schedules_bounded_retry_without_provider_attempt(monkeypatch):
    patch, send = setup(monkeypatch)
    main.generate_follow_up_result.side_effect = RuntimeError("No model")
    assert asyncio.run(main.execute_follow_up_job(job(attempt_count=1), now=NOW)) == "scheduled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "scheduled"
    assert patch.await_args.args[1]["next_retry_at"] == (NOW + timedelta(minutes=1)).isoformat()


def test_read_outage_before_generation_schedules_safe_retry(monkeypatch):
    patch, send = setup(monkeypatch)
    main.get_conversation_by_id.side_effect = RuntimeError("Database temporarily unavailable")
    assert asyncio.run(main.execute_follow_up_job(job(attempt_count=1), now=NOW)) == "scheduled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["next_retry_at"] == (NOW + timedelta(minutes=1)).isoformat()


def test_retry_waits_until_persisted_retry_time(monkeypatch):
    patch, send = setup(monkeypatch)
    future = NOW + timedelta(minutes=2)
    assert asyncio.run(main.execute_follow_up_job(job(next_retry_at=future.isoformat()), now=NOW)) == "scheduled"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "scheduled"


def test_preparation_exhaustion_is_visible_without_provider_attempt(monkeypatch):
    patch, send = setup(monkeypatch)
    main.generate_follow_up_result.side_effect = RuntimeError("No model")
    assert asyncio.run(main.execute_follow_up_job(job(attempt_count=3), now=NOW)) == "failed"
    send.assert_not_awaited()
    assert patch.await_args.args[1]["status"] == "failed"
    assert "after 3 attempts" in patch.await_args.args[1]["last_error"]


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


def test_test_service_can_run_follow_up_without_scheduled_reply_worker(monkeypatch):
    monkeypatch.setenv("BACKEND_WORKERS_ENABLED", "true")
    monkeypatch.setenv("SCHEDULED_REPLY_WORKER_ENABLED", "false")
    follow_up_started = asyncio.Event()
    scheduled_reply = AsyncMock()

    async def held_follow_up():
        follow_up_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main, "scheduled_reply_worker", scheduled_reply)
    monkeypatch.setattr(main, "follow_up_worker", held_follow_up)

    async def check():
        async with main.app_lifespan(main.app):
            await asyncio.wait_for(follow_up_started.wait(), timeout=1)
            scheduled_reply.assert_not_awaited()

    asyncio.run(check())


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
