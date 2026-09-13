from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ai_generation import AiGenerationUsage
from config import DEFAULT_ANTHROPIC_SETTER_MODEL, DEFAULT_OPENAI_SETTER_MODEL


@dataclass(frozen=True)
class PricingRates:
    provider: str
    model: str
    exchange_rate_to_eur: float
    usd: dict[str, float]
    eur: dict[str, float]


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def pricing_rates(provider: str, model: str) -> PricingRates:
    provider = (provider or "").lower()
    model_lower = (model or "").lower()
    if provider == "openai":
        if DEFAULT_OPENAI_SETTER_MODEL not in model_lower:
            raise ValueError(f"No versioned OpenAI pricing configured for model {model!r}")
        exchange = _env_float("OPENAI_USD_TO_EUR_RATE", 0.86088154)
        usd = {"input": 0.20, "output": 1.20, "cache_creation": 0.25, "cache_read": 0.02}
        eur = {
            "input": _env_float("GPT_5_6_LUNA_INPUT_EUR_PER_MTOKEN", usd["input"] * exchange),
            "output": _env_float("GPT_5_6_LUNA_OUTPUT_EUR_PER_MTOKEN", usd["output"] * exchange),
            "cache_creation": _env_float("GPT_5_6_LUNA_CACHE_CREATION_INPUT_EUR_PER_MTOKEN", usd["cache_creation"] * exchange),
            "cache_read": _env_float("GPT_5_6_LUNA_CACHED_INPUT_EUR_PER_MTOKEN", usd["cache_read"] * exchange),
        }
        return PricingRates("openai", model or DEFAULT_OPENAI_SETTER_MODEL, exchange, usd, eur)

    if provider != "anthropic":
        raise ValueError(f"No pricing configured for AI provider {provider!r}")

    exchange = _env_float("ANTHROPIC_USD_TO_EUR_RATE", 0.86088154)
    if "opus-4-7" in model_lower:
        usd = {"input": 5.0, "output": 25.0, "cache_creation": 6.25, "cache_read": 0.5}
        prefix = "CLAUDE_OPUS_4_7"
    else:
        usd = {"input": 3.0, "output": 15.0, "cache_creation": 3.75, "cache_read": 0.3}
        prefix = "CLAUDE_SONNET_4_6"
    eur = {
        "input": _env_float(f"{prefix}_INPUT_EUR_PER_MTOKEN", usd["input"] * exchange),
        "output": _env_float(f"{prefix}_OUTPUT_EUR_PER_MTOKEN", usd["output"] * exchange),
        "cache_creation": _env_float(f"{prefix}_CACHE_CREATION_INPUT_EUR_PER_MTOKEN", usd["cache_creation"] * exchange),
        "cache_read": _env_float(f"{prefix}_CACHE_READ_INPUT_EUR_PER_MTOKEN", usd["cache_read"] * exchange),
    }
    return PricingRates("anthropic", model or DEFAULT_ANTHROPIC_SETTER_MODEL, exchange, usd, eur)


def _token_quantities(usage: AiGenerationUsage) -> dict[str, int]:
    input_tokens = usage.input_tokens or 0
    cache_creation = usage.cache_creation_input_tokens or 0
    cache_read = usage.cache_read_input_tokens or 0
    if usage.provider == "openai":
        input_tokens = max(0, input_tokens - cache_creation - cache_read)
    return {
        "input": input_tokens,
        "output": usage.output_tokens or 0,
        "cache_creation": cache_creation,
        "cache_read": cache_read,
    }


def calculate_usage_cost(usage: AiGenerationUsage, currency: str = "EUR") -> float:
    rates = pricing_rates(usage.provider, usage.model)
    selected = rates.eur if currency.upper() == "EUR" else rates.usd
    quantities = _token_quantities(usage)
    multipliers = {"input": 1.0, "output": 1.0, "cache_creation": 1.0, "cache_read": 1.0}
    if usage.provider == "openai" and (usage.input_tokens or 0) > 272_000:
        multipliers.update({"input": 2.0, "output": 1.5, "cache_creation": 2.0, "cache_read": 2.0})
    return sum(
        quantities[key] / 1_000_000 * selected[key] * multipliers[key]
        for key in quantities
    )


def estimate_usage_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    usage = AiGenerationUsage(
        provider=provider,
        provider_response_id=None,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        usage_raw=None,
    )
    return calculate_usage_cost(usage, "EUR")


def pricing_snapshot(provider: str, model: str, pricing_version: str) -> dict[str, Any]:
    rates = pricing_rates(provider, model)
    return {
        "provider": rates.provider,
        "model": rates.model,
        "provider_currency": "USD",
        "cost_currency": "EUR",
        "rates_per_million_tokens": rates.eur,
        "provider_rates_usd_per_million_tokens": rates.usd,
        "exchange_rate_to_eur": rates.exchange_rate_to_eur,
        "pricing_version": pricing_version,
        "source": "Versioned deployment pricing derived from provider list prices",
        "long_context_pricing": {
            "input_token_threshold": 272000,
            "input_multiplier": 2.0,
            "output_multiplier": 1.5,
        } if provider == "openai" else None,
    }
