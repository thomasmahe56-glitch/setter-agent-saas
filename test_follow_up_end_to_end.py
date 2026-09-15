import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main


@pytest.mark.parametrize("preparation_failure", [False, True])
def test_cloud_worker_pipeline_plans_then_sends_once_with_virtual_time(monkeypatch, preparation_failure):
    start = datetime(2026, 9, 15, 14, 59, tzinfo=timezone.utc)
    anchor = start - timedelta(hours=6, minutes=59)
    inbound = anchor - timedelta(minutes=30)
    tenant = "tenant-test"
    conversation_id = "conversation-test"
    conversation = {
        "id": conversation_id, "user_id": tenant, "channel": "instagram",
        "automation_mode": "auto", "agent_active": True,
        "human_takeover": False, "contact_status": "active", "status": "en_cours",
        "last_inbound_at": inbound.isoformat(),
        "history": [
            {"role": "user", "content": "Test inbound", "timestamp": inbound.isoformat()},
            {"role": "assistant", "content": "Test outbound", "timestamp": anchor.isoformat(), "sent": True},
        ],
    }
    settings = {
        "follow_up_config_version": 1, "timezone": "Europe/Paris",
        "allowed_send_start": "08:00", "allowed_send_end": "20:00",
        "follow_up_config": [{"stage": "follow_up_1", "enabled": True,
            "delay_value": 7, "delay_unit": "hours", "mode": "auto"}],
    }
    queue = [{"conversation_id": conversation_id, "user_id": tenant,
        "queued_at": start.isoformat(), "claimed_at": None}]
    jobs = []
    clock = {"now": start}

    class Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False

        async def get(self, url, *, params=None, **kwargs):
            if url == main.SUPABASE_FOLLOW_UP_QUEUE_URL:
                return Response([dict(row) for row in queue if row["claimed_at"] is None])
            if url == main.SUPABASE_FOLLOW_UP_JOBS_URL:
                return Response([dict(row) for row in jobs if row["status"] in {"scheduled", "processing"}])
            raise AssertionError(f"Unexpected GET: {url}")

        async def post(self, url, *, json, **kwargs):
            if url.endswith("release_stale_follow_up_refresh_claims") or url.endswith("reconcile_stale_follow_up_processing"):
                return Response(0)
            if url.endswith("claim_due_follow_up_jobs"):
                claimed = []
                for row in jobs:
                    runnable = row.get("next_retry_at") or row["scheduled_at"]
                    if row["status"] == "scheduled" and datetime.fromisoformat(runnable) <= clock["now"]:
                        row["status"] = "processing"
                        row["attempt_count"] = row.get("attempt_count", 0) + 1
                        claimed.append(dict(row))
                return Response(claimed)
            if url == main.SUPABASE_FOLLOW_UP_JOBS_URL:
                for row in json:
                    jobs.append({**row, "id": f"job-{len(jobs) + 1}", "attempt_count": 0})
                return Response([])
            raise AssertionError(f"Unexpected POST: {url}")

        async def patch(self, url, *, json, **kwargs):
            if url == main.SUPABASE_FOLLOW_UP_QUEUE_URL:
                queue[0]["claimed_at"] = json["claimed_at"]
                return Response([{"conversation_id": conversation_id}])
            if url == main.SUPABASE_CONVERSATIONS_URL:
                conversation.update(json)
                return Response([{"id": conversation_id}])
            raise AssertionError(f"Unexpected PATCH: {url}")

        async def delete(self, url, **kwargs):
            assert url == main.SUPABASE_FOLLOW_UP_QUEUE_URL
            queue.clear()
            return Response([])

    async def patch_job(job_id, values, *, expected_status=None):
        row = next(row for row in jobs if row["id"] == job_id)
        if expected_status and row["status"] != expected_status:
            return False
        row.update(values)
        return True

    real_execute = main.execute_follow_up_job

    async def execute_at_virtual_time(row):
        return await real_execute(row, now=clock["now"])

    provider = AsyncMock(return_value={"status_code": 200, "body": "{}"})
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    monkeypatch.setattr(main, "get_conversation_by_id", AsyncMock(return_value=conversation))
    monkeypatch.setattr(main, "get_follow_up_settings_strict", AsyncMock(return_value=settings))
    monkeypatch.setattr(main, "patch_follow_up_job", patch_job)
    monkeypatch.setattr(main, "execute_follow_up_job", execute_at_virtual_time)
    monkeypatch.setattr(main, "enforce_ai_cost_cap", AsyncMock())
    generated = SimpleNamespace(text="Virtual follow-up")
    monkeypatch.setattr(main, "generate_follow_up_result", AsyncMock(
        side_effect=[RuntimeError("Temporary AI outage"), generated] if preparation_failure else None,
        return_value=generated,
    ))
    monkeypatch.setattr(main, "record_ai_usage_event", AsyncMock())
    monkeypatch.setattr(main, "send_channel_message", provider)

    async def run():
        assert await main.process_follow_up_refresh_queue() == 1
        assert queue == []
        assert len(jobs) == 1
        assert jobs[0]["due_at"] == (start + timedelta(minutes=1)).isoformat()
        assert jobs[0]["scheduled_at"] == jobs[0]["due_at"]
        assert jobs[0]["status"] == "scheduled"
        assert await main.process_due_follow_up_jobs() == 0
        provider.assert_not_awaited()

        clock["now"] += timedelta(minutes=1)
        assert await main.process_due_follow_up_jobs() == 1
        if preparation_failure:
            assert jobs[0]["status"] == "scheduled"
            assert jobs[0]["next_retry_at"] == (clock["now"] + timedelta(minutes=1)).isoformat()
            provider.assert_not_awaited()
            assert await main.process_due_follow_up_jobs() == 0
            clock["now"] += timedelta(minutes=1)
            assert await main.process_due_follow_up_jobs() == 1
        assert jobs[0]["status"] == "sent"
        assert conversation["history"][-1]["follow_up_job_id"] == jobs[0]["id"]
        assert conversation["history"][-1]["sent"] is True
        provider.assert_awaited_once()
        assert provider.await_args.kwargs["at_most_once"] is True

        assert await main.process_due_follow_up_jobs() == 0
        assert provider.await_count == 1

    asyncio.run(run())
