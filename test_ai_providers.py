import asyncio
import json
import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("SUPABASE_URL", "http://localhost/rest/v1")
os.environ.setdefault("SUPABASE_KEY", "test-supabase-key")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DASHBOARD_SECRET", "test-dashboard-secret")

import main
from ai_generation import ProviderGenerationError, extract_openai_usage
from ai_pricing import calculate_usage_cost, pricing_rates, pricing_snapshot


class _FakeOpenAIResponses:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            id="resp_luna_1",
            model="gpt-5.6-luna",
            output_text="Short setter reply",
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=120,
                input_tokens_details=SimpleNamespace(cached_tokens=400, cache_write_tokens=100),
                output_tokens_details=SimpleNamespace(reasoning_tokens=20),
            ),
        )


class _FakeAnthropicMessages:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            id="msg_fallback_1",
            model="claude-sonnet-4-6",
            content=[SimpleNamespace(type="text", text="Premium fallback reply")],
            usage=SimpleNamespace(input_tokens=90, output_tokens=20),
        )


def test_primary_route_uses_configured_openai_luna(monkeypatch):
    responses = _FakeOpenAIResponses()
    monkeypatch.setattr(main.config, "setter_openai_enabled", True)
    monkeypatch.setattr(main.config, "setter_primary_provider", "openai")
    monkeypatch.setattr(main.config, "setter_primary_model", "gpt-5.6-luna")
    monkeypatch.setattr(main, "openai_client", SimpleNamespace(responses=responses))

    result = main.generate_ai_generation(
        [{"role": "user", "content": "Hello"}],
        "Setter rules",
        max_output_tokens=384,
        reasoning_level="none",
    )

    assert result.text == "Short setter reply"
    assert result.usage.provider == "openai"
    assert result.usage.model == "gpt-5.6-luna"
    assert result.usage.cache_read_input_tokens == 400
    assert result.usage.cache_creation_input_tokens == 100
    assert result.usage.reasoning_tokens == 20
    assert result.usage.metadata["latency_ms"] >= 0
    assert responses.requests[0]["max_output_tokens"] == 384
    assert responses.requests[0]["reasoning"] == {"effort": "none"}
    assert responses.requests[0]["store"] is False


def test_openai_disabled_falls_back_to_configured_anthropic_route(monkeypatch):
    monkeypatch.setattr(main.config, "setter_openai_enabled", False)
    monkeypatch.setattr(main.config, "setter_primary_provider", "openai")
    monkeypatch.setattr(main.config, "setter_primary_model", "gpt-5.6-luna")
    monkeypatch.setattr(main.config, "setter_premium_provider", "anthropic")
    monkeypatch.setattr(main.config, "setter_premium_model", "claude-sonnet-4-6")
    assert main.select_setter_route(reasoning_level="low") == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "reasoning_level": None,
    }


def test_missing_openai_key_falls_back_to_configured_anthropic_route(monkeypatch):
    monkeypatch.setattr(main.config, "setter_openai_enabled", True)
    monkeypatch.setattr(main.config, "setter_primary_provider", "openai")
    monkeypatch.setattr(main.config, "setter_primary_model", "gpt-5.6-luna")
    monkeypatch.setattr(main.config, "setter_premium_provider", "anthropic")
    monkeypatch.setattr(main.config, "setter_premium_model", "claude-sonnet-4-6")
    monkeypatch.setattr(main, "openai_client", None)
    assert main.select_setter_route(reasoning_level="low") == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "reasoning_level": None,
    }


def test_anthropic_primary_preserves_previous_messages_api_behavior(monkeypatch):
    anthropic_messages = _FakeAnthropicMessages()
    monkeypatch.setattr(main.config, "setter_primary_provider", "anthropic")
    monkeypatch.setattr(main.config, "setter_primary_model", "claude-sonnet-4-6")
    monkeypatch.setattr(main, "client", SimpleNamespace(messages=anthropic_messages))

    result = main.generate_ai_generation(
        [{"role": "user", "content": "Hello"}],
        "Setter rules",
        max_output_tokens=384,
        reasoning_level="high",
    )

    assert result.text == "Premium fallback reply"
    assert result.usage.provider == "anthropic"
    assert anthropic_messages.requests == [{
        "model": "claude-sonnet-4-6",
        "max_tokens": 384,
        "system": "Setter rules",
        "messages": [{"role": "user", "content": "Hello"}],
    }]


def test_premium_route_is_only_selected_when_explicitly_enabled(monkeypatch):
    monkeypatch.setattr(main.config, "setter_openai_enabled", True)
    monkeypatch.setattr(main.config, "setter_primary_provider", "openai")
    monkeypatch.setattr(main.config, "setter_primary_model", "gpt-5.6-luna")
    monkeypatch.setattr(main.config, "setter_premium_provider", "anthropic")
    monkeypatch.setattr(main.config, "setter_premium_model", "claude-sonnet-4-6")
    monkeypatch.setattr(main.config, "setter_premium_escalation_enabled", False)
    monkeypatch.setattr(main, "openai_client", SimpleNamespace(responses=_FakeOpenAIResponses()))
    assert main.select_setter_route(premium_requested=True)["provider"] == "openai"
    monkeypatch.setattr(main.config, "setter_premium_escalation_enabled", True)
    assert main.select_setter_route(premium_requested=True) == main.premium_route()


def test_primary_provider_failure_can_fallback_without_a_router_llm_call(monkeypatch):
    class FailingResponses:
        def create(self, **_kwargs):
            raise RuntimeError("429 rate limit")

    anthropic_messages = _FakeAnthropicMessages()
    monkeypatch.setattr(main.config, "setter_openai_enabled", True)
    monkeypatch.setattr(main.config, "setter_primary_provider", "openai")
    monkeypatch.setattr(main.config, "setter_primary_model", "gpt-5.6-luna")
    monkeypatch.setattr(main.config, "setter_premium_provider", "anthropic")
    monkeypatch.setattr(main.config, "setter_premium_model", "claude-sonnet-4-6")
    monkeypatch.setattr(main.config, "setter_premium_escalation_enabled", True)
    monkeypatch.setattr(main, "openai_client", SimpleNamespace(responses=FailingResponses()))
    monkeypatch.setattr(main, "client", SimpleNamespace(messages=anthropic_messages))

    result = main.generate_ai_generation(
        [{"role": "user", "content": "Hello"}],
        "Setter rules",
        max_output_tokens=384,
        reasoning_level="none",
    )

    assert result.text == "Premium fallback reply"
    assert result.usage.provider == "anthropic"
    assert result.usage.metadata["fallback_from_provider"] == "openai"
    assert result.usage.metadata["fallback_reason"] == "provider_rate_limit"
    assert len(anthropic_messages.requests) == 1


def test_provider_error_is_exposed_when_fallback_is_disabled(monkeypatch):
    class FailingResponses:
        def create(self, **_kwargs):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(main.config, "setter_openai_enabled", True)
    monkeypatch.setattr(main.config, "setter_primary_provider", "openai")
    monkeypatch.setattr(main.config, "setter_primary_model", "gpt-5.6-luna")
    monkeypatch.setattr(main.config, "setter_premium_escalation_enabled", False)
    monkeypatch.setattr(main, "openai_client", SimpleNamespace(responses=FailingResponses()))

    with pytest.raises(ProviderGenerationError) as caught:
        main.generate_ai_generation(
            [{"role": "user", "content": "Hello"}],
            "Setter rules",
            max_output_tokens=384,
        )
    assert caught.value.provider == "openai"
    assert caught.value.error_type == "provider_error"


def test_luna_pricing_handles_uncached_cached_cache_write_and_output(monkeypatch):
    monkeypatch.setenv("OPENAI_USD_TO_EUR_RATE", "1")
    monkeypatch.setenv("GPT_5_6_LUNA_INPUT_EUR_PER_MTOKEN", "0.20")
    monkeypatch.setenv("GPT_5_6_LUNA_OUTPUT_EUR_PER_MTOKEN", "1.20")
    monkeypatch.setenv("GPT_5_6_LUNA_CACHE_CREATION_INPUT_EUR_PER_MTOKEN", "0.25")
    monkeypatch.setenv("GPT_5_6_LUNA_CACHED_INPUT_EUR_PER_MTOKEN", "0.02")
    usage = main.AiGenerationUsage(
        provider="openai",
        provider_response_id="resp_cost",
        model="gpt-5.6-luna",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=100_000,
        cache_read_input_tokens=200_000,
        reasoning_tokens=50_000,
        usage_raw={},
    )
    # Luna applies its long-context tier to the whole request above 272k input tokens.
    assert calculate_usage_cost(usage, "EUR") == pytest.approx(2.138)
    snapshot = pricing_snapshot("openai", "gpt-5.6-luna", "pricing-v1")
    assert snapshot["pricing_version"] == "pricing-v1"
    assert snapshot["rates_per_million_tokens"]["cache_read"] == pytest.approx(0.02)
    assert snapshot["long_context_pricing"]["input_token_threshold"] == 272_000


def test_unknown_provider_never_inherits_anthropic_pricing():
    with pytest.raises(ValueError, match="No pricing configured"):
        pricing_rates("unknown-provider", "unknown-model")


def test_openai_usage_extraction_accepts_dict_payload():
    response = SimpleNamespace(
        id="resp_dict",
        model="gpt-5.6-luna",
        usage={
            "input_tokens": 80,
            "output_tokens": 20,
            "input_tokens_details": {"cached_tokens": 30, "cache_write_tokens": 10},
            "output_tokens_details": {"reasoning_tokens": 5},
        },
    )
    usage = extract_openai_usage(response, "low")
    assert (usage.input_tokens, usage.output_tokens) == (80, 20)
    assert (usage.cache_read_input_tokens, usage.cache_creation_input_tokens) == (30, 10)
    assert usage.reasoning_tokens == 5
    assert usage.reasoning_level == "low"


def test_ai_usage_writes_openai_event_directly_to_usage_ledger(monkeypatch):
    captured = {}

    class Response:
        status_code = 201
        text = ""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            captured.update({"url": url, **kwargs})
            return Response()

    async def no_release(_user_id):
        return None

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    monkeypatch.setattr(main, "release_ai_cost_slot", no_release)
    usage = main.AiGenerationUsage(
        provider="openai",
        provider_response_id="resp_ledger",
        model="gpt-5.6-luna",
        input_tokens=1000,
        output_tokens=100,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=250,
        reasoning_tokens=12,
        reasoning_level="none",
        usage_raw={"total_tokens": 1100},
    )
    row = asyncio.run(main.record_ai_usage_event(
        "tenant-a",
        "inbound_reply",
        json.dumps([{"role": "user", "content": "Hi"}]),
        "Hello",
        usage=usage,
        conversation_id="conversation-a",
        request_kind="inbound_reply",
    ))

    assert captured["url"] == main.SUPABASE_USAGE_LEDGER_URL
    assert row["provider"] == "openai"
    assert row["model"] == "gpt-5.6-luna"
    assert row["provider_event_id"] == "resp_ledger"
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 100
    assert row["cache_read_input_tokens"] == 250
    assert row["reasoning_tokens"] == 12
    assert row["provider_cost"] == pytest.approx(0.000275)
    assert row["cost_eur"] == round(0.000275 * 0.86088154, 8)
    assert row["pricing_snapshot"]["provider"] == "openai"
    assert row["cost_accuracy"] == "provider_usage_priced"
    assert row["idempotency_key"] == "openai:resp_ledger"


def test_ai_usage_retries_use_one_tenant_scoped_ledger_identity(monkeypatch):
    inserted = set()
    accepted_rows = []

    class Response:
        status_code = 201
        text = ""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **kwargs):
            row = kwargs["json"]
            key = (row["user_id"], row["idempotency_key"])
            if key not in inserted:
                inserted.add(key)
                accepted_rows.append(row)
            return Response()

    async def no_release(_user_id):
        return None

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    monkeypatch.setattr(main, "release_ai_cost_slot", no_release)
    usage = main.AiGenerationUsage(
        provider="openai",
        provider_response_id="resp_same",
        model="gpt-5.6-luna",
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        usage_raw={},
    )

    async def scenario():
        first = await main.record_ai_usage_event("tenant-a", "inbound_reply", "Hi", "Hello", usage=usage)
        second = await main.record_ai_usage_event("tenant-a", "inbound_reply", "Hi", "Hello", usage=usage)
        third = await main.record_ai_usage_event("tenant-b", "inbound_reply", "Hi", "Hello", usage=usage)
        return first, second, third

    rows = asyncio.run(scenario())
    assert [row["idempotency_key"] for row in rows] == ["openai:resp_same"] * 3
    assert len(accepted_rows) == 2
    assert {row["user_id"] for row in accepted_rows} == {"tenant-a", "tenant-b"}
