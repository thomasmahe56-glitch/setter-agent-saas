import asyncio
from datetime import datetime, timezone

import pytest

import main
from usage_economics import (
    aggregate_costs,
    build_cost_activities,
    build_credit_debit,
    credit_summary,
    month_period,
    safe_unit_cost,
    select_credit_rule,
    summarize_cost_activities,
)


def test_current_month_range_is_half_open_utc():
    start, end = month_period(datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc))
    assert start.isoformat() == "2026-12-01T00:00:00+00:00"
    assert end.isoformat() == "2027-01-01T00:00:00+00:00"


def test_cost_aggregation_keeps_unknown_separate_from_zero():
    result = aggregate_costs([
        {"module": "setter", "feature": "reply", "cost_accuracy": "provider_usage_priced", "cost_eur": 1.25, "input_tokens": 10},
        {"module": "prospecting", "feature": "scrape", "provider": "apify", "cost_accuracy": "provider_reported", "cost_eur": 2.75, "unit": "run"},
        {"module": "prospecting", "feature": "scrape", "provider": "apify", "cost_accuracy": "unknown", "cost_eur": None, "unit": "run"},
    ])
    assert result["total_cost_eur"] == 4.0
    assert result["untracked_operations"] == 1
    assert result["tracking_coverage_percent"] == pytest.approx(66.67)
    assert result["provider_reported_share_percent"] == pytest.approx(68.75)
    assert safe_unit_cost(result["total_cost_eur"], 0) is None


def test_anthropic_provider_usage_pricing_and_legacy_parity():
    usage = main.AiGenerationUsage(
        provider="anthropic",
        model="claude-opus-4-7",
        provider_response_id="msg_test",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        usage_raw={},
    )
    cost = main.calculate_anthropic_usage_cost_eur(usage)
    assert cost == pytest.approx(main.CLAUDE_OPUS_4_7_INPUT_EUR_PER_MTOKEN + main.CLAUDE_OPUS_4_7_OUTPUT_EUR_PER_MTOKEN)
    assert main.calculate_anthropic_usage_cost_usd(usage) == pytest.approx(30.0)
    legacy_row = {"cost_accuracy": "provider_usage_priced", "cost_eur": cost, "module": "setter", "feature": "feedback_loop"}
    assert aggregate_costs([legacy_row])["total_cost_eur"] == pytest.approx(cost)


def test_credit_engine_disabled_and_enabled_rule_is_versioned_idempotent():
    assert credit_summary([], False) == {
        "credits_enabled": False,
        "credits_used": None,
        "credits_remaining": None,
        "credits_included": None,
    }
    occurred = datetime(2026, 9, 11, tzinfo=timezone.utc)
    event = {"id": "usage-1", "user_id": "tenant-a", "module": "setter", "event_type": "assistant_reply_generated", "quantity": 2}
    rule = {
        "id": "rule-1", "module": "setter", "event_type": "assistant_reply_generated",
        "credits_per_unit": 3, "version": "v1", "enabled": True,
        "effective_from": "2026-09-01T00:00:00Z", "effective_to": None,
    }
    selected = select_credit_rule([rule], event, occurred)
    debit = build_credit_debit(event, selected)
    assert debit is not None
    assert debit["credits"] == -6.0
    assert debit["idempotency_key"] == "usage_debit:usage-1:v1"
    assert build_credit_debit(event, selected) == debit


def test_admin_authorization_is_server_side(monkeypatch):
    monkeypatch.setattr(main.config, "owner_user_id", "tenant-admin")
    assert asyncio.run(main.require_admin("tenant-admin")) == "tenant-admin"
    with pytest.raises(main.HTTPException) as error:
        asyncio.run(main.require_admin("tenant-b"))
    assert error.value.status_code == 403


def test_client_usage_summary_is_tenant_scoped_and_hides_internal_costs(monkeypatch):
    captured = {}

    async def fake_rows(*, start, end, user_id=None, limit=10000):
        captured["user_id"] = user_id
        return ([
            {
                "user_id": "tenant-a", "module": "setter", "event_type": "assistant_reply_generated",
                "quantity": 2, "cost_accuracy": "provider_usage_priced", "cost_eur": 99,
            },
            {
                "user_id": "tenant-a", "module": "prospecting", "event_type": "source_discovery",
                "quantity": 3, "cost_accuracy": "unknown", "cost_eur": None,
            },
        ], True)

    async def fake_credits(user_id, start, end):
        return credit_summary([], False)

    monkeypatch.setattr(main, "fetch_usage_rows", fake_rows)
    monkeypatch.setattr(main, "get_credit_state", fake_credits)
    result = asyncio.run(main.get_usage_summary("tenant-a"))
    assert captured["user_id"] == "tenant-a"
    assert result["usage"]["source_discoveries"] == 3
    assert result["prospecting"]["source_discoveries"] == 3
    assert result["usage"]["assistant_replies"] == 2
    assert result["credits_remaining"] is None
    assert "total_cost_eur" not in result
    assert "cost_by_provider" not in result


def test_usage_reader_paginates_past_supabase_row_cap(monkeypatch):
    offsets = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, params, **_kwargs):
            offset = int(params["offset"])
            offsets.append(offset)
            return FakeResponse([{"quantity": 1}] * (1000 if offset == 0 else 1))

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)
    start, end = month_period(datetime(2026, 9, 11, tzinfo=timezone.utc))
    rows, available = asyncio.run(main.fetch_usage_rows(start=start, end=end))
    assert available is True
    assert len(rows) == 1001
    assert offsets == [0, 1000]


def test_cost_activities_group_a_discovery_run_and_keep_messages_individual():
    rows = [
        {
            "id": "source", "user_id": "tenant-a", "run_id": "run-1", "module": "prospecting",
            "feature": "source_discovery", "event_type": "source_discovery", "provider": "tavily",
            "quantity": 1, "cost_accuracy": "provider_reported", "cost_eur": "0.02",
            "occurred_at": "2026-09-11T09:00:00Z",
        },
        {
            "id": "profiles", "user_id": "tenant-a", "run_id": "run-1", "module": "prospecting",
            "feature": "instagram_scrape", "event_type": "profiles_retrieved", "provider": "apify",
            "quantity": 25, "cost_accuracy": "provider_reported", "cost_eur": "0.18",
            "occurred_at": "2026-09-11T09:01:00Z",
        },
        {
            "id": "reply-1", "user_id": "tenant-a", "module": "setter", "conversation_id": "conversation-1",
            "feature": "reply", "event_type": "assistant_reply_generated", "provider": "anthropic",
            "provider_event_id": "msg-1", "quantity": 1, "cost_accuracy": "provider_usage_priced", "cost_eur": "0.05",
            "occurred_at": "2026-09-11T10:00:00Z",
        },
        {
            "id": "reply-2", "user_id": "tenant-a", "module": "setter", "conversation_id": "conversation-1",
            "feature": "reply", "event_type": "assistant_reply_generated", "provider": "anthropic",
            "provider_event_id": "msg-2", "quantity": 1, "cost_accuracy": "unknown", "cost_eur": None,
            "occurred_at": "2026-09-11T10:05:00Z",
        },
    ]
    activities = build_cost_activities(rows)
    assert len(activities) == 3
    discovery = next(item for item in activities if item["activity_type"] == "discovery")
    assert discovery["event_count"] == 2
    assert discovery["total_cost_eur"] == pytest.approx(0.2)
    assert discovery["cost_complete"] is True
    assert discovery["prospects_retrieved"] == 25
    assert discovery["known_cost_per_prospect_eur"] == pytest.approx(0.008)
    replies = [item for item in activities if item["activity_type"] == "assistant_message"]
    assert len(replies) == 2
    assert sum(item["unknown_operations"] for item in replies) == 1

    summaries = summarize_cost_activities(activities)
    assert summaries["discovery"]["activity_count"] == 1
    assert summaries["discovery"]["average_known_cost_eur"] == pytest.approx(0.2)
    assert summaries["discovery"]["known_cost_per_prospect_eur"] == pytest.approx(0.008)
    assert summaries["assistant_message"]["activity_count"] == 2
    assert summaries["assistant_message"]["average_known_cost_eur"] == pytest.approx(0.025)
    assert summaries["assistant_message"]["coverage_percent"] == pytest.approx(50)


def test_admin_activity_history_is_server_side_and_filterable(monkeypatch):
    async def fake_rows(*, start, end, user_id=None, limit=100000):
        return ([{
            "id": "row-1", "user_id": "tenant-a", "run_id": "run-1", "module": "prospecting",
            "feature": "source_discovery", "event_type": "source_discovery", "provider": "tavily",
            "quantity": 1, "cost_accuracy": "provider_reported", "cost_eur": "0.03",
            "occurred_at": "2026-09-11T09:00:00Z",
        }], True)

    monkeypatch.setattr(main, "fetch_usage_rows", fake_rows)
    result = asyncio.run(main.get_admin_economics_activities(
        days=90, activity_type="discovery", limit=10, offset=0, _admin_user_id="tenant-admin",
    ))
    assert result["period"]["days"] == 90
    assert result["total_activities"] == 1
    assert result["activities"][0]["activity_type"] == "discovery"
    assert result["summaries"]["discovery"]["average_known_cost_eur"] == pytest.approx(0.03)
