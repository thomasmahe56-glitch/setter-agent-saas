from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from config import DEFAULT_ANTHROPIC_SETTER_MODEL, DEFAULT_OPENAI_SETTER_MODEL


GENERIC_AI_USER_MESSAGE = "Angellos couldn’t generate a reply right now. Please check your AI provider billing or try again."


class ProviderGenerationError(Exception):
    def __init__(
        self,
        provider: str,
        error_type: str,
        message: str,
        user_message: str = GENERIC_AI_USER_MESSAGE,
        status_code: int = 502,
    ):
        super().__init__(message)
        self.provider = provider
        self.error_type = error_type
        self.message = message
        self.user_message = user_message
        self.status_code = status_code


@dataclass
class AiGenerationUsage:
    provider: str
    provider_response_id: Optional[str]
    model: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cache_creation_input_tokens: Optional[int]
    cache_read_input_tokens: Optional[int]
    usage_raw: Optional[dict[str, Any]]
    reasoning_tokens: Optional[int] = None
    reasoning_level: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AiGenerationResult:
    text: str
    usage: Optional[AiGenerationUsage] = None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _object_to_plain_json(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    try:
        def encode(item: Any):
            if hasattr(item, "model_dump"):
                return item.model_dump()
            if hasattr(item, "__dict__"):
                return vars(item)
            return str(item)

        plain = json.loads(json.dumps(value, default=encode))
        return plain if isinstance(plain, dict) else None
    except Exception:
        return None


def extract_anthropic_usage(response: Any, reasoning_level: Optional[str] = None) -> AiGenerationUsage:
    usage_obj = getattr(response, "usage", None)
    usage_raw = _object_to_plain_json(usage_obj)
    usage_source = usage_raw if isinstance(usage_raw, dict) else {}
    return AiGenerationUsage(
        provider="anthropic",
        provider_response_id=getattr(response, "id", None),
        model=str(getattr(response, "model", "") or DEFAULT_ANTHROPIC_SETTER_MODEL),
        input_tokens=_coerce_int(usage_source.get("input_tokens", getattr(usage_obj, "input_tokens", None))),
        output_tokens=_coerce_int(usage_source.get("output_tokens", getattr(usage_obj, "output_tokens", None))),
        cache_creation_input_tokens=_coerce_int(
            usage_source.get("cache_creation_input_tokens", getattr(usage_obj, "cache_creation_input_tokens", None))
        ),
        cache_read_input_tokens=_coerce_int(
            usage_source.get("cache_read_input_tokens", getattr(usage_obj, "cache_read_input_tokens", None))
        ),
        usage_raw=usage_raw,
        reasoning_tokens=_coerce_int(usage_source.get("reasoning_tokens")),
        reasoning_level=reasoning_level,
    )


def extract_openai_usage(response: Any, reasoning_level: Optional[str] = None) -> AiGenerationUsage:
    usage_obj = getattr(response, "usage", None)
    usage_raw = _object_to_plain_json(usage_obj)
    usage_source = usage_raw if isinstance(usage_raw, dict) else {}
    input_details = usage_source.get("input_tokens_details") or {}
    output_details = usage_source.get("output_tokens_details") or {}
    cache_write_tokens = (
        input_details.get("cache_write_tokens")
        or usage_source.get("cache_write_tokens")
        or 0
    )
    return AiGenerationUsage(
        provider="openai",
        provider_response_id=getattr(response, "id", None),
        model=str(getattr(response, "model", "") or DEFAULT_OPENAI_SETTER_MODEL),
        input_tokens=_coerce_int(usage_source.get("input_tokens", getattr(usage_obj, "input_tokens", None))),
        output_tokens=_coerce_int(usage_source.get("output_tokens", getattr(usage_obj, "output_tokens", None))),
        cache_creation_input_tokens=_coerce_int(cache_write_tokens),
        cache_read_input_tokens=_coerce_int(input_details.get("cached_tokens")),
        usage_raw=usage_raw,
        reasoning_tokens=_coerce_int(
            output_details.get("reasoning_tokens") or usage_source.get("reasoning_tokens")
        ),
        reasoning_level=reasoning_level,
    )


def classify_provider_error(provider: str, error: Exception) -> ProviderGenerationError:
    provider = (provider or "unknown").strip().lower()
    provider_label = "OpenAI" if provider == "openai" else "Anthropic" if provider == "anthropic" else provider
    text = str(error)
    lowered = text.lower()
    print(f"[provider:classify] provider={provider} {type(error).__name__}: {text[:500]}", flush=True)
    if any(marker in lowered for marker in ["credit balance", "credits", "billing", "insufficient_credit", "insufficient_quota"]):
        return ProviderGenerationError(
            provider,
            "provider_billing",
            f"{provider_label} billing unavailable: {text[:300]}",
            f"Angellos couldn’t generate a reply because {provider_label} API billing is unavailable. Check that provider account, then try again.",
            status_code=402,
        )
    if "rate" in lowered or "429" in lowered:
        return ProviderGenerationError(provider, "provider_rate_limit", f"{provider_label} rate limit: {text[:300]}", status_code=429)
    if "timeout" in lowered or "timed out" in lowered:
        return ProviderGenerationError(provider, "provider_timeout", f"{provider_label} timeout: {text[:300]}", status_code=504)
    if "not_found" in lowered or "404" in lowered:
        return ProviderGenerationError(provider, "provider_model_not_found", f"{provider_label} model not found: {text[:300]}")
    if "invalid request" in lowered or "badrequest" in lowered or "400" in lowered:
        return ProviderGenerationError(provider, "provider_invalid_request", f"{provider_label} rejected request: {text[:300]}")
    return ProviderGenerationError(provider, "provider_error", f"{provider_label} error ({type(error).__name__}): {text[:300]}")


def generate_ai_generation(
    messages: list,
    system_prompt: str = "",
    *,
    provider: str,
    model: str,
    max_output_tokens: int,
    reasoning_level: Optional[str] = None,
    anthropic_client: Any = None,
    openai_client: Any = None,
) -> AiGenerationResult:
    provider = (provider or "").strip().lower()
    started_at = time.perf_counter()
    try:
        if provider == "anthropic":
            if anthropic_client is None:
                raise ProviderGenerationError(
                    provider,
                    "provider_not_configured",
                    "ANTHROPIC_API_KEY is not configured",
                    "Angellos couldn’t generate a reply because the Anthropic API is not configured.",
                    status_code=500,
                )
            response = anthropic_client.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                system=system_prompt,
                messages=messages,
            )
            content = getattr(response, "content", []) or []
            text_block = next((block for block in content if getattr(block, "type", None) == "text"), content[0] if content else None)
            usage = extract_anthropic_usage(response, reasoning_level)
            usage.metadata["latency_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
            return AiGenerationResult(
                text=str(getattr(text_block, "text", "")),
                usage=usage,
            )

        if provider == "openai":
            if openai_client is None:
                raise ProviderGenerationError(
                    provider,
                    "provider_not_configured",
                    "OPENAI_API_KEY is not configured",
                    "Angellos couldn’t generate a reply because the OpenAI API is not configured.",
                    status_code=500,
                )
            request: dict[str, Any] = {
                "model": model,
                "input": messages,
                "max_output_tokens": max_output_tokens,
                "store": False,
            }
            if system_prompt:
                request["instructions"] = system_prompt
            if reasoning_level:
                request["reasoning"] = {"effort": reasoning_level}
            response = openai_client.responses.create(**request)
            usage = extract_openai_usage(response, reasoning_level)
            usage.metadata["latency_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
            return AiGenerationResult(
                text=str(getattr(response, "output_text", "") or ""),
                usage=usage,
            )

        raise ProviderGenerationError(
            provider or "unknown",
            "provider_not_supported",
            f"Unsupported AI provider: {provider or 'empty'}",
            status_code=500,
        )
    except ProviderGenerationError:
        raise
    except Exception as error:
        raise classify_provider_error(provider, error) from error
