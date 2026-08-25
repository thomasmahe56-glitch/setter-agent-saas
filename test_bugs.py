"""Focused regression tests for BUG 1 (placeholder display names) and BUG 2 (supervised activation)."""
import asyncio
import json
import types
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers imported directly from main (no server startup needed)
# ---------------------------------------------------------------------------
import importlib, sys, os

# Stub required env / dependencies before importing main
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("SUPABASE_URL", "http://localhost/rest/v1")
os.environ.setdefault("SUPABASE_KEY", "test-supabase-key")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DASHBOARD_SECRET", "test-dashboard-secret")

# Patch anthropic.Anthropic so import doesn't fail without real creds
import anthropic as _anthropic_mod  # already installed

from main import (
    ANGELLOS_BETA_REPLY_RULES,
    WebhookPayload,
    build_generation_prompt,
    build_structured_refinement_result,
    build_prompt_diff,
    build_training_center_prompt,
    default_automation_mode_for_prompt,
    durable_rule_from_refinement_instruction,
    extract_manychat_ig_username,
    flush_pending_deliveries,
    handle_inbound_message,
    is_manychat_pending_delivery_error,
    is_placeholder_display_name,
    is_real_instagram_username,
    normalize_display_name,
    get_angellos_beta_canned_reply,
    is_angellos_acquisition_prompt,
    learn_refinement_rule,
    merge_rule_list,
    tenant_language_from_prompt,
    _needs_supervised_pending,
    _last_prospect_message,
    webhook,
    bulk_update_automation_mode,
    BulkAutomationModePayload,
    CostCapExceededError,
    RefinePromptPayload,
    refine_prompt,
    estimate_token_count,
    estimate_claude_cost_eur,
    enforce_ai_cost_cap,
    configured_follow_up_stage,
    is_within_allowed_send_window,
    next_allowed_send_at,
    normalize_beta_account_settings,
    SIMULATOR_SCENARIOS,
    deterministic_simulator_reply,
    run_simulator_scenario,
    score_simulated_reply,
    judge_simulated_reply_quality,
    QUALITY_JUDGE_DECISIONS,
    QUALITY_JUDGE_SCORE_KEYS,
)


class _PatchResponse:
    status_code = 204
    text = ""

    def raise_for_status(self):
        return None


class _PatchCaptureAsyncClient:
    patches = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def patch(self, *args, **kwargs):
        self.__class__.patches.append({"args": args, "kwargs": kwargs})
        return _PatchResponse()


# ===========================================================================
# BUG 1 — Display name placeholder detection
# ===========================================================================

class TestIsPlaceholderDisplayName:
    def test_instagram_prospect_placeholder(self):
        assert is_placeholder_display_name("Instagram prospect") is True

    def test_unknown_placeholder(self):
        assert is_placeholder_display_name("unknown") is True

    def test_ig_username_placeholder(self):
        assert is_placeholder_display_name("{{ig_username}}") is True

    def test_username_placeholder(self):
        assert is_placeholder_display_name("{{username}}") is True

    def test_first_name_placeholder(self):
        assert is_placeholder_display_name("{{first_name}}") is True

    def test_arbitrary_placeholder(self):
        assert is_placeholder_display_name("{{some_var}}") is True

    def test_numeric_manychat_id_rejected(self):
        # ManyChat subscriber IDs are long numeric strings
        assert is_placeholder_display_name("612751574") is True
        assert is_placeholder_display_name("1168444660") is True
        assert is_placeholder_display_name("370303411") is True

    def test_short_number_not_rejected(self):
        # 5-digit numbers could be part of a legitimate handle
        assert is_placeholder_display_name("12345") is False

    def test_valid_name_not_placeholder(self):
        assert is_placeholder_display_name("julien_runs") is False

    def test_real_name_not_placeholder(self):
        assert is_placeholder_display_name("Jean Dupont") is False

    def test_handle_with_digits_not_placeholder(self):
        assert is_placeholder_display_name("coach2025") is False

    def test_empty_string_not_placeholder(self):
        assert is_placeholder_display_name("") is False

    def test_none_not_placeholder(self):
        assert is_placeholder_display_name(None) is False

    def test_partial_placeholder_detected(self):
        assert is_placeholder_display_name("Hello {{ig_username}}") is True


class TestNormalizeDisplayNameNumericId:
    def test_numeric_id_rejected_returns_fallback(self):
        assert normalize_display_name("612751574", external_contact_id="612751574") == "Unresolved Instagram contact 1574"

    def test_numeric_id_keeps_existing_valid_name(self):
        assert normalize_display_name("612751574", existing="julien_runs") == "julien_runs"

    def test_numeric_id_with_numeric_existing_returns_fallback(self):
        assert normalize_display_name("612751574", existing="612751574", external_contact_id="612751574") == "Unresolved Instagram contact 1574"


class TestNormalizeDisplayName:
    def test_valid_incoming_returned_as_is(self):
        assert normalize_display_name("julien_runs") == "julien_runs"

    def test_placeholder_rejected_returns_fallback(self):
        result = normalize_display_name("{{ig_username}}", external_contact_id="123456789")
        assert result == "Unresolved Instagram contact 6789"

    def test_placeholder_keeps_existing_valid_name(self):
        result = normalize_display_name("{{ig_username}}", existing="julien_runs")
        assert result == "julien_runs"

    def test_placeholder_existing_also_placeholder_returns_fallback(self):
        result = normalize_display_name("{{ig_username}}", existing="{{ig_username}}", external_contact_id="123456789")
        assert result == "Unresolved Instagram contact 6789"

    def test_none_incoming_existing_valid_returns_existing(self):
        result = normalize_display_name(None, existing="julien_runs")
        assert result == "julien_runs"

    def test_none_incoming_no_existing_returns_fallback(self):
        result = normalize_display_name(None, existing=None, external_contact_id="123456789")
        assert result == "Unresolved Instagram contact 6789"

    def test_valid_incoming_overrides_existing(self):
        result = normalize_display_name("new_handle", existing="old_handle")
        assert result == "new_handle"

    def test_strips_whitespace(self):
        assert normalize_display_name("  julien_runs  ") == "julien_runs"


class TestManyChatUsernameExtraction:
    def test_extracts_direct_ig_username(self):
        assert extract_manychat_ig_username({"data": {"ig_username": "real_handle"}}) == "real_handle"

    def test_extracts_instagram_username(self):
        assert extract_manychat_ig_username({"data": {"instagram_username": "@real.handle"}}) == "real.handle"

    def test_extracts_custom_field_username(self):
        payload = {"data": {"custom_fields": [{"name": "Instagram username", "value": "coach_2026"}]}}
        assert extract_manychat_ig_username(payload) == "coach_2026"

    def test_rejects_placeholder_and_numeric_candidates(self):
        payload = {"data": {"ig_username": "Instagram prospect", "username": "123456789"}}
        assert extract_manychat_ig_username(payload) is None

    def test_real_instagram_username_rejects_numeric_id(self):
        assert is_real_instagram_username("123456789") is False
        assert is_real_instagram_username("coach2026") is True


# ===========================================================================
# BUG 2 — Supervised activation logic
# ===========================================================================

def _make_conversation(**kwargs) -> dict:
    defaults = {
        "id": "conv-1",
        "user_id": "user-1",
        "automation_mode": "supervised",
        "agent_active": True,
        "pending_message": None,
        "history": [],
    }
    return {**defaults, **kwargs}


def _user_msg(content: str = "Hello") -> dict:
    return {"role": "user", "content": content, "timestamp": "2026-06-01T10:00:00Z"}


def _assistant_msg(content: str = "Hi!", sent: bool = False, ignored: bool = False) -> dict:
    return {"role": "assistant", "content": content, "sent": sent, "ignored": ignored}


class TestLastProspectMessage:
    def test_returns_last_user_message(self):
        history = [_user_msg("first"), _assistant_msg(), _user_msg("last")]
        msg = _last_prospect_message(history)
        assert msg["content"] == "last"

    def test_returns_none_for_empty_history(self):
        assert _last_prospect_message([]) is None

    def test_returns_none_when_only_assistant(self):
        assert _last_prospect_message([_assistant_msg()]) is None


class TestNeedsSupervisedPending:
    def test_supervised_unanswered_prospect_needs_pending(self):
        conv = _make_conversation(history=[_user_msg("Hey")])
        assert _needs_supervised_pending(conv) is True

    def test_auto_mode_does_not_need_pending(self):
        conv = _make_conversation(automation_mode="auto", history=[_user_msg("Hey")])
        assert _needs_supervised_pending(conv) is False

    def test_disabled_mode_does_not_need_pending(self):
        conv = _make_conversation(automation_mode="disabled", history=[_user_msg("Hey")])
        assert _needs_supervised_pending(conv) is False

    def test_already_has_pending_does_not_need_more(self):
        conv = _make_conversation(
            history=[_user_msg("Hey")],
            pending_message="Already here",
        )
        assert _needs_supervised_pending(conv) is False

    def test_unsent_not_ignored_assistant_at_end_does_not_need_pending(self):
        conv = _make_conversation(history=[_user_msg("Hey"), _assistant_msg(sent=False)])
        assert _needs_supervised_pending(conv) is False

    def test_ignored_assistant_at_end_still_needs_pending(self):
        # Ignored message → prospect still unanswered
        conv = _make_conversation(history=[_user_msg("Hey"), _assistant_msg(sent=False, ignored=True)])
        assert _needs_supervised_pending(conv) is True

    def test_empty_history_does_not_need_pending(self):
        conv = _make_conversation(history=[])
        assert _needs_supervised_pending(conv) is False

    def test_only_assistant_history_does_not_need_pending(self):
        conv = _make_conversation(history=[_assistant_msg(sent=True)])
        assert _needs_supervised_pending(conv) is False

    def test_sent_assistant_after_user_still_needs_pending(self):
        # Last message is a sent assistant reply → prospect hasn't replied yet
        # So there's nothing to reply to
        conv = _make_conversation(
            history=[_user_msg("Hey"), _assistant_msg(sent=True)]
        )
        # Last message is assistant (sent=True), not user → no pending needed
        assert _needs_supervised_pending(conv) is False

    def test_multiple_exchanges_unanswered_needs_pending(self):
        history = [
            _user_msg("q1"), _assistant_msg(sent=True),
            _user_msg("q2"),
        ]
        conv = _make_conversation(history=history)
        assert _needs_supervised_pending(conv) is True


# ===========================================================================
# BUG 3 — ManyChat webhook auto mode sends directly from backend
# ===========================================================================

class TestManyChatWebhookAutoSend:
    def test_webhook_direct_sends_and_does_not_return_duplicate_agent_response(self, monkeypatch):
        captured = {}

        async def fake_require_secret(secret):
            return "user-1"

        async def fake_handle_inbound_message(**kwargs):
            captured.update(kwargs)
            return {
                "reply": "Auto reply",
                "sent": True,
                "should_send": True,
                "mode": "auto",
                "skipped": False,
                "reason": None,
                "send_result": {"status_code": 200, "body": "ok"},
                "conversation_id": "conv-1",
            }

        monkeypatch.setattr("main.require_secret", fake_require_secret)
        monkeypatch.setattr("main.handle_inbound_message", fake_handle_inbound_message)

        response = asyncio.run(webhook(
            WebhookPayload(username="real_handle", message="Hello", subscriber_id="123456789"),
            x_webhook_secret="test-secret",
        ))

        assert captured["auto_send_transport"] is True
        assert response["should_send"] is True
        assert response["sent"] is True
        assert response["automation_mode"] == "auto"
        assert response["agent_response"] == ""

    def test_webhook_returns_agent_response_when_backend_send_fails(self, monkeypatch):
        async def fake_require_secret(secret):
            return "user-1"

        async def fake_handle_inbound_message(**kwargs):
            return {
                "reply": "Fallback reply",
                "sent": False,
                "should_send": True,
                "mode": "auto",
                "skipped": False,
                "reason": None,
                "send_result": {"status_code": 400, "body": "outside 24h"},
                "conversation_id": "conv-1",
            }

        monkeypatch.setattr("main.require_secret", fake_require_secret)
        monkeypatch.setattr("main.handle_inbound_message", fake_handle_inbound_message)

        response = asyncio.run(webhook(
            WebhookPayload(username="real_handle", message="Hello", subscriber_id="123456789"),
            x_webhook_secret="test-secret",
        ))

        assert response["should_send"] is True
        assert response["sent"] is False
        assert response["agent_response"] == "Fallback reply"

    def test_auto_mode_canned_reply_is_sent_and_marked_sent(self, monkeypatch):
        _PatchCaptureAsyncClient.patches = []

        async def fake_get_contact_by_external_id(*args, **kwargs):
            return {
                "id": "conv-1",
                "user_id": "user-1",
                "channel": "instagram",
                "external_contact_id": "subscriber-123",
                "username": "subscriber-123",
                "display_name": "prospect_handle",
                "automation_mode": "auto",
                "agent_active": True,
                "history": [],
            }

        async def fake_get_active_prompt(user_id):
            return build_training_center_prompt("Base prompt", {"is_angellos_acquisition": True}, {}, {})

        async def fake_send_channel_message(conversation, text):
            assert text == "No worries, appreciate you getting back to me."
            return {"status_code": 200, "body": "ok"}

        monkeypatch.setattr("main.get_contact_by_external_id", fake_get_contact_by_external_id)
        monkeypatch.setattr("main.get_active_prompt", fake_get_active_prompt)
        monkeypatch.setattr("main.send_channel_message", fake_send_channel_message)
        monkeypatch.setattr("main.httpx.AsyncClient", _PatchCaptureAsyncClient)

        result = asyncio.run(handle_inbound_message(
            channel="instagram",
            external_contact_id="subscriber-123",
            display_name="prospect_handle",
            message="No thanks bro",
            user_id="user-1",
            transport_metadata={"provider": "manychat", "message_id": "msg-1"},
            auto_send_transport=True,
        ))

        assert result["should_send"] is True
        assert result["sent"] is True
        assert result["mode"] == "auto"
        assert _PatchCaptureAsyncClient.patches[-1]["kwargs"]["json"]["history"][-1]["sent"] is True

    def test_auto_mode_3011_marks_reply_pending_delivery(self, monkeypatch):
        _PatchCaptureAsyncClient.patches = []

        async def fake_get_contact_by_external_id(*args, **kwargs):
            return {
                "id": "conv-1",
                "user_id": "user-1",
                "channel": "instagram",
                "external_contact_id": "subscriber-123",
                "username": "subscriber-123",
                "display_name": "prospect_handle",
                "automation_mode": "auto",
                "agent_active": True,
                "history": [],
            }

        async def fake_get_active_prompt(user_id):
            return build_training_center_prompt("Base prompt", {"is_angellos_acquisition": True}, {}, {})

        async def fake_send_channel_message(conversation, text):
            return {"status_code": 400, "body": '{"status":"error","code":3011,"message":"outside 24h window"}'}

        monkeypatch.setattr("main.get_contact_by_external_id", fake_get_contact_by_external_id)
        monkeypatch.setattr("main.get_active_prompt", fake_get_active_prompt)
        monkeypatch.setattr("main.send_channel_message", fake_send_channel_message)
        monkeypatch.setattr("main.httpx.AsyncClient", _PatchCaptureAsyncClient)

        result = asyncio.run(handle_inbound_message(
            channel="instagram",
            external_contact_id="subscriber-123",
            display_name="prospect_handle",
            message="No thanks bro",
            user_id="user-1",
            transport_metadata={"provider": "manychat", "message_id": "msg-3011"},
            auto_send_transport=True,
        ))

        final_patch = _PatchCaptureAsyncClient.patches[-1]["kwargs"]["json"]
        assistant = final_patch["history"][-1]
        assert result["should_send"] is True
        assert result["sent"] is False
        assert final_patch["status"] == "pending_delivery"
        assert final_patch["pending_message"] == "No worries, appreciate you getting back to me."
        assert assistant["pending_delivery"] is True
        assert assistant["delivery_status"] == "pending_delivery"


class TestPendingDeliveryHelpers:
    def test_detects_manychat_3011_json_body(self):
        assert is_manychat_pending_delivery_error({
            "status_code": 400,
            "body": '{"status":"error","code":3011,"message":"outside window"}',
        }) is True

    def test_flush_pending_delivery_marks_existing_message_sent_without_appending(self, monkeypatch):
        _PatchCaptureAsyncClient.patches = []
        conversation = {
            "id": "conv-1",
            "user_id": "user-1",
            "channel": "instagram",
            "external_contact_id": "subscriber-123",
            "history": [
                {"role": "user", "content": "hello", "timestamp": "2026-07-09T10:00:00Z"},
                {
                    "role": "assistant",
                    "content": "pending reply",
                    "timestamp": "2026-07-09T10:01:00Z",
                    "sent": False,
                    "pending_delivery": True,
                    "delivery_failed": True,
                },
            ],
        }

        async def fake_send_channel_message(conv, text):
            assert text == "pending reply"
            return {"status_code": 200, "body": "ok"}

        monkeypatch.setattr("main.send_channel_message", fake_send_channel_message)
        monkeypatch.setattr("main.httpx.AsyncClient", _PatchCaptureAsyncClient)

        result = asyncio.run(flush_pending_deliveries(conversation, "user-1"))
        patch_body = _PatchCaptureAsyncClient.patches[-1]["kwargs"]["json"]
        assert result["flushed"] == 1
        assert len(patch_body["history"]) == 2
        assert patch_body["history"][-1]["sent"] is True
        assert patch_body["history"][-1]["delivery_status"] == "sent_after_inbound_retry"
        assert patch_body["pending_message"] is None
        assert patch_body["status"] == "en_cours"


class TestTenantFirstFrenchPrompt:
    def _tenant_prompt(self, **profile_overrides):
        profile = {
            "language": "fr",
            "business_name": "Cabinet Croissance Kiné",
            "niche": "consultants growth français pour cabinets de kinés",
            "offer_name": "Sprint Acquisition Patients",
            "offer_promise": "remplir l'agenda avec des demandes qualifiées",
            "price": "2500 EUR",
            "calendly_url": "https://calendly.com/client/demo",
            "next_step": "proposer un audit de 20 minutes",
            **profile_overrides,
        }
        return build_training_center_prompt("BASE PROMPT", profile, {"persona_summary": "kiné premium"}, {"qualification_questions": ["budget", "ville"]})

    def test_final_tenant_prompt_contains_training_center_data_and_french_instruction(self):
        prompt = build_generation_prompt(self._tenant_prompt())
        assert "Sprint Acquisition Patients" in prompt
        assert "2500 EUR" in prompt
        assert "Default tenant language: French" in prompt
        assert "Write natural, short French by default" in prompt

    def test_tenant_prompt_excludes_angellos_beta_canned_lines(self):
        prompt = build_generation_prompt(self._tenant_prompt())
        forbidden = [
            "For the beta, it’s free for 30 days",
            "Angellos is an AI setter",
            "Want me to send the beta page",
        ]
        for line in forbidden:
            assert line not in prompt

    def test_unconfigured_prompt_defaults_to_tenant_mode(self):
        prompt = build_generation_prompt("BASE PROMPT")
        assert "TENANT CLIENT MODE" in prompt
        assert "ANGELLOS BETA MARKET SETTINGS" not in prompt
        assert is_angellos_acquisition_prompt("BASE PROMPT") is False

    def test_acquisition_prompt_still_contains_beta_rules(self):
        prompt = build_generation_prompt(self._tenant_prompt(is_angellos_acquisition=True))
        assert ANGELLOS_BETA_REPLY_RULES in prompt
        assert "For the beta, it’s free for 30 days" in prompt

    def test_canned_replies_are_available_only_for_acquisition_mode(self):
        tenant_prompt = self._tenant_prompt()
        acquisition_prompt = self._tenant_prompt(is_angellos_acquisition=True)
        assert is_angellos_acquisition_prompt(tenant_prompt) is False
        assert is_angellos_acquisition_prompt(acquisition_prompt) is True
        assert get_angellos_beta_canned_reply("what is Angellos?") is not None
        assert "Angellos is an AI setter" not in build_generation_prompt(tenant_prompt)

    def test_prompt_builder_respects_per_profile_language_and_mode(self):
        tenant_prompt = self._tenant_prompt(language="fr")
        acquisition_prompt = self._tenant_prompt(language="en", agent_use_case="acquisition")
        assert tenant_language_from_prompt(tenant_prompt) == "fr"
        assert "TENANT CLIENT MODE" in build_generation_prompt(tenant_prompt)
        assert "ANGELLOS BETA MARKET SETTINGS" in build_generation_prompt(acquisition_prompt)

    def test_tenant_beta_conversations_default_supervised(self):
        assert default_automation_mode_for_prompt(self._tenant_prompt()) == "supervised"

    def test_acquisition_conversations_keep_auto_default(self):
        assert default_automation_mode_for_prompt(self._tenant_prompt(is_angellos_acquisition=True)) == "auto"


class TestRefinePendingLearning:
    def test_price_instruction_becomes_durable_sales_rule(self):
        rule = durable_rule_from_refinement_instruction(
            "Ne donne jamais le prix directement, il faut le garder pour l'appel de vente."
        )

        assert rule == (
            "Ne jamais donner le prix directement en DM. Si le prospect demande le prix, "
            "répondre que le tarif dépend du contexte et qu'il sera confirmé pendant l'audit/appel, "
            "puis poser une question de qualification simple."
        )

    def test_generic_instruction_is_kept_as_short_rule(self):
        assert durable_rule_from_refinement_instruction("Rends la réponse plus chaleureuse") == "Rends la réponse plus chaleureuse"

    def test_merge_rule_list_deduplicates_case_insensitively(self):
        assert merge_rule_list(["Ne donne jamais le prix"], " ne DONNE jamais le prix ") == [
            "Ne donne jamais le prix"
        ]

    def test_learn_refinement_rule_updates_sales_rules_and_creates_prompt_version(self):
        calls = {"get": [], "upsert": [], "active_prompt": 0}

        async def fake_get_user_singleton_row(table_url, user_id, select="*"):
            calls["get"].append((table_url, user_id, select))
            if table_url.endswith("/agent_sales_rules"):
                return {"rules": {"do_not_say": ["Pas de hype"], "qualification_questions": ["budget"]}}
            if table_url.endswith("/agent_profiles"):
                return {"profile": {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"}}
            if table_url.endswith("/agent_avatars"):
                return {"avatar": {"persona_summary": "coach français"}}
            return None

        async def fake_upsert_user_singleton_row(table_url, user_id, payload):
            calls["upsert"].append((table_url, user_id, payload))
            return {"id": "rules-row", **payload, "user_id": user_id}

        async def fake_get_active_prompt(user_id):
            calls["active_prompt"] += 1
            return "BASE PROMPT"

        class _PromptResponse:
            def __init__(self, payload=None):
                self._payload = payload or []

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _PromptClient:
            posts = []
            patches = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def patch(self, *args, **kwargs):
                self.__class__.patches.append({"args": args, "kwargs": kwargs})
                return _PromptResponse()

            async def post(self, *args, **kwargs):
                self.__class__.posts.append({"args": args, "kwargs": kwargs})
                return _PromptResponse([{"id": "prompt-version-1"}])

        _PromptClient.posts = []
        _PromptClient.patches = []

        with patch("main.get_user_singleton_row", fake_get_user_singleton_row), \
            patch("main.upsert_user_singleton_row", fake_upsert_user_singleton_row), \
            patch("main.get_active_prompt", fake_get_active_prompt), \
            patch("main.httpx.AsyncClient", _PromptClient):
            result = asyncio.run(
                learn_refinement_rule(
                    "user-123",
                    "Ne donne jamais le prix directement, garde ça pour l'appel de vente.",
                )
            )

        assert result["learned"] is True
        assert result["prompt_version_id"] == "prompt-version-1"
        assert calls["active_prompt"] == 1
        saved_rules = calls["upsert"][0][2]["rules"]
        assert "Pas de hype" in saved_rules["do_not_say"]
        assert result["rule"] in saved_rules["do_not_say"]
        assert result["rule"] in saved_rules["objection_responses"]
        assert "Avant de parler tarif" in saved_rules["qualification_questions"][-1]
        created_prompt = _PromptClient.posts[0]["kwargs"]["json"]
        assert created_prompt["source"].startswith("refine-learn:")
        assert result["rule"] in created_prompt["content"]


class TestTrainingCenterRefinePromptRobustness:
    production_instruction = (
        "This reply needs improvement:\n"
        "\"Le tarif dépend vraiment du contexte de ton cabinet on le confirme après un rapide audit de 20 minutes.\n\n"
        "Tu es kiné ou ostéo ?\""
    )
    price_qualification_rule = "Avant de parler tarif, qualifier le type de besoin, la situation actuelle et l'urgence du prospect."

    def _active_prompt(self):
        return build_training_center_prompt(
            "BASE PROMPT",
            {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"},
            {"persona_summary": "cabinet kiné"},
            {"do_not_say": ["Pas de hype"]},
        )

    def test_structured_preview_does_not_persist_and_builds_diff(self, monkeypatch):
        upserts = []

        async def fake_get_user_singleton_row(table_url, user_id, select="*"):
            if table_url.endswith("/agent_profiles"):
                return {"profile": {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"}}
            if table_url.endswith("/agent_avatars"):
                return {"avatar": {"persona_summary": "cabinet kiné"}}
            if table_url.endswith("/agent_sales_rules"):
                return {"rules": {"do_not_say": ["Pas de hype"]}}
            return None

        async def fake_upsert_user_singleton_row(table_url, user_id, payload):
            upserts.append((table_url, user_id, payload))
            return payload

        monkeypatch.setattr("main.get_user_singleton_row", fake_get_user_singleton_row)
        monkeypatch.setattr("main.upsert_user_singleton_row", fake_upsert_user_singleton_row)

        result = asyncio.run(build_structured_refinement_result(
            "user-123",
            self.production_instruction,
            self._active_prompt(),
            apply=False,
        ))
        diff = build_prompt_diff(self._active_prompt(), result["updated_prompt"])

        assert result["structured_fallback"] is True
        assert "tarif dépend" in result["updated_prompt"]
        assert any(item["type"] == "add" for item in diff)
        assert upserts == []

    def test_structured_preview_fr_handles_quotes_newlines_without_claude(self, monkeypatch):
        class _ExplodingMessages:
            def create(self, **kwargs):
                raise AssertionError("Claude should not be called for Training Center structured refinement")

        class _ExplodingClient:
            messages = _ExplodingMessages()

        async def fake_get_active_prompt_version(user_id):
            return {"id": "active-1", "content": self._active_prompt(), "is_active": True}

        async def fake_get_user_singleton_row(table_url, user_id, select="*"):
            if table_url.endswith("/agent_profiles"):
                return {"profile": {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"}}
            if table_url.endswith("/agent_avatars"):
                return {"avatar": {"persona_summary": "cabinet kiné"}}
            if table_url.endswith("/agent_sales_rules"):
                return {"rules": {"do_not_say": ["Pas de hype"]}}
            return None

        monkeypatch.setattr("main.client", _ExplodingClient())
        monkeypatch.setattr("main.get_active_prompt_version", fake_get_active_prompt_version)
        monkeypatch.setattr("main.get_user_singleton_row", fake_get_user_singleton_row)

        result = asyncio.run(refine_prompt(
            RefinePromptPayload(instruction=self.production_instruction, apply=False),
            user_id="user-123",
        ))

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["applied"] is False
        assert result["already_learned"] is False
        assert result["target_section"] == "Règles commerciales du Training Center"
        assert "règle durable" in result["summary"]
        assert any("Règle ajoutée" in item for item in result["changes"])
        assert any(item["type"] == "add" for item in result["diff"])

    def test_truncated_claude_json_falls_back_without_exposing_jsondecodeerror(self, monkeypatch):
        calls = {"claude": 0}

        class _FakeMessages:
            def create(self, **kwargs):
                calls["claude"] += 1
                return types.SimpleNamespace(content=[types.SimpleNamespace(text='{"updated_prompt": "abc')])

        class _FakeClient:
            messages = _FakeMessages()

        async def fake_get_active_prompt_version(user_id):
            return {"id": "active-1", "content": self._active_prompt(), "is_active": True}

        async def fake_get_user_singleton_row(table_url, user_id, select="*"):
            if table_url.endswith("/agent_profiles"):
                return {"profile": {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"}}
            if table_url.endswith("/agent_avatars"):
                return {"avatar": {"persona_summary": "cabinet kiné"}}
            if table_url.endswith("/agent_sales_rules"):
                return {"rules": {"do_not_say": ["Pas de hype"]}}
            return None

        monkeypatch.setattr("main.client", _FakeClient())
        monkeypatch.setattr("main.get_active_prompt_version", fake_get_active_prompt_version)
        monkeypatch.setattr("main.get_user_singleton_row", fake_get_user_singleton_row)

        result = asyncio.run(refine_prompt(
            RefinePromptPayload(instruction=self.production_instruction, apply=False),
            user_id="user-123",
        ))

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["applied"] is False
        assert result["diff"]
        assert "JSONDecodeError" not in json.dumps(result)
        assert "tarif dépend" in result["updated_prompt"]
        assert calls["claude"] == 0

    def test_preview_already_present_rule_returns_already_learned_without_error(self, monkeypatch):
        rule = durable_rule_from_refinement_instruction(self.production_instruction)
        saved_rules = {
            "do_not_say": [rule],
            "objection_responses": [rule],
            "qualification_questions": [self.price_qualification_rule],
        }

        async def fake_get_user_singleton_row(table_url, user_id, select="*"):
            if table_url.endswith("/agent_profiles"):
                return {"profile": {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"}}
            if table_url.endswith("/agent_avatars"):
                return {"avatar": {"persona_summary": "cabinet kiné"}}
            if table_url.endswith("/agent_sales_rules"):
                return {"rules": saved_rules}
            return None

        active_prompt = build_training_center_prompt(
            "BASE PROMPT",
            {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"},
            {"persona_summary": "cabinet kiné"},
            saved_rules,
        )
        monkeypatch.setattr("main.get_user_singleton_row", fake_get_user_singleton_row)

        result = asyncio.run(build_structured_refinement_result(
            "user-123",
            self.production_instruction,
            active_prompt,
            apply=False,
        ))
        diff = build_prompt_diff(active_prompt, result["updated_prompt"])

        assert result["already_learned"] is True
        assert result["rules_changed"] is False
        assert result["summary"] == "Cette règle est déjà enregistrée dans le Training Center."
        assert diff == []

    def test_apply_prompt_proposed_same_as_current_is_idempotent_no_prompt_version(self, monkeypatch):
        rule = durable_rule_from_refinement_instruction(self.production_instruction)
        saved_rules = {
            "do_not_say": [rule],
            "objection_responses": [rule],
            "qualification_questions": [self.price_qualification_rule],
        }
        active_prompt = build_training_center_prompt(
            "BASE PROMPT",
            {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"},
            {"persona_summary": "cabinet kiné"},
            saved_rules,
        )

        class _PromptClient:
            patches = []
            posts = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def patch(self, *args, **kwargs):
                self.__class__.patches.append({"args": args, "kwargs": kwargs})
                raise AssertionError("No prompt version should be deactivated for an idempotent apply")

            async def post(self, *args, **kwargs):
                self.__class__.posts.append({"args": args, "kwargs": kwargs})
                raise AssertionError("No prompt version should be inserted for an idempotent apply")

        async def fake_get_active_prompt_version(user_id):
            return {"id": "active-1", "content": active_prompt, "is_active": True}

        async def fake_get_user_singleton_row(table_url, user_id, select="*"):
            if table_url.endswith("/agent_profiles"):
                return {"profile": {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"}}
            if table_url.endswith("/agent_avatars"):
                return {"avatar": {"persona_summary": "cabinet kiné"}}
            if table_url.endswith("/agent_sales_rules"):
                return {"rules": saved_rules}
            return None

        async def fake_upsert_user_singleton_row(table_url, user_id, payload):
            raise AssertionError("Already learned rule should not be upserted again")

        monkeypatch.setattr("main.get_active_prompt_version", fake_get_active_prompt_version)
        monkeypatch.setattr("main.get_user_singleton_row", fake_get_user_singleton_row)
        monkeypatch.setattr("main.upsert_user_singleton_row", fake_upsert_user_singleton_row)
        monkeypatch.setattr("main.httpx.AsyncClient", _PromptClient)

        result = asyncio.run(refine_prompt(
            RefinePromptPayload(
                instruction=self.production_instruction,
                apply=True,
                prompt_proposed=active_prompt,
            ),
            user_id="user-123",
        ))

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["applied"] is True
        assert result["already_learned"] is True
        assert result["prompt_version_id"] is None
        assert _PromptClient.patches == []
        assert _PromptClient.posts == []

    def test_apply_true_persists_rule_and_prompt_version_after_fallback(self, monkeypatch):
        upserts = []

        class _FakeMessages:
            def create(self, **kwargs):
                return types.SimpleNamespace(content=[types.SimpleNamespace(text='{"updated_prompt": "abc')])

        class _FakeClient:
            messages = _FakeMessages()

        class _PromptResponse:
            def __init__(self, payload=None):
                self._payload = payload or []
                self.status_code = 200
                self.text = json.dumps(self._payload)

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _PromptClient:
            patches = []
            posts = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def patch(self, *args, **kwargs):
                self.__class__.patches.append({"args": args, "kwargs": kwargs})
                return _PromptResponse()

            async def post(self, *args, **kwargs):
                self.__class__.posts.append({"args": args, "kwargs": kwargs})
                return _PromptResponse([{"id": "prompt-version-2"}])

        async def fake_get_active_prompt_version(user_id):
            return {"id": "active-1", "content": self._active_prompt(), "is_active": True}

        async def fake_get_user_singleton_row(table_url, user_id, select="*"):
            if table_url.endswith("/agent_profiles"):
                return {"profile": {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"}}
            if table_url.endswith("/agent_avatars"):
                return {"avatar": {"persona_summary": "cabinet kiné"}}
            if table_url.endswith("/agent_sales_rules"):
                return {"rules": {"do_not_say": ["Pas de hype"]}}
            return None

        async def fake_upsert_user_singleton_row(table_url, user_id, payload):
            upserts.append((table_url, user_id, payload))
            return {"id": "rules-row", **payload}

        _PromptClient.patches = []
        _PromptClient.posts = []
        monkeypatch.setattr("main.client", _FakeClient())
        monkeypatch.setattr("main.get_active_prompt_version", fake_get_active_prompt_version)
        monkeypatch.setattr("main.get_user_singleton_row", fake_get_user_singleton_row)
        monkeypatch.setattr("main.upsert_user_singleton_row", fake_upsert_user_singleton_row)
        monkeypatch.setattr("main.httpx.AsyncClient", _PromptClient)

        result = asyncio.run(refine_prompt(
            RefinePromptPayload(instruction=self.production_instruction, apply=True),
            user_id="user-123",
        ))

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["applied"] is True
        assert result["prompt_version_id"] == "prompt-version-2"
        assert upserts[0][0].endswith("/agent_sales_rules")
        saved_rules = upserts[0][2]["rules"]
        assert any("tarif dépend" in item for item in saved_rules["do_not_say"])
        created_prompt = _PromptClient.posts[0]["kwargs"]["json"]
        assert created_prompt["previous_version_id"] == "active-1"
        assert "JSONDecodeError" not in json.dumps(result)

    def test_apply_prompt_proposed_persists_preview_without_regenerating_prompt(self, monkeypatch):
        upserts = []
        active_prompt = self._active_prompt()
        proposed_prompt = f"{active_prompt}\n\nRègle preview validée: ne pas donner le tarif directement."

        class _PromptResponse:
            def __init__(self, payload=None):
                self._payload = payload or []
                self.status_code = 200
                self.text = json.dumps(self._payload)

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _PromptClient:
            patches = []
            posts = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def patch(self, *args, **kwargs):
                self.__class__.patches.append({"args": args, "kwargs": kwargs})
                return _PromptResponse()

            async def post(self, *args, **kwargs):
                self.__class__.posts.append({"args": args, "kwargs": kwargs})
                return _PromptResponse([{"id": "prompt-version-preview"}])

        async def fake_get_active_prompt_version(user_id):
            return {"id": "active-1", "content": active_prompt, "is_active": True}

        async def fake_get_user_singleton_row(table_url, user_id, select="*"):
            if table_url.endswith("/agent_profiles"):
                return {"profile": {"language": "fr", "offer_name": "Audit Growth", "price": "2500 EUR"}}
            if table_url.endswith("/agent_avatars"):
                return {"avatar": {"persona_summary": "cabinet kiné"}}
            if table_url.endswith("/agent_sales_rules"):
                return {"rules": {"do_not_say": ["Pas de hype"]}}
            return None

        async def fake_upsert_user_singleton_row(table_url, user_id, payload):
            upserts.append((table_url, user_id, payload))
            return {"id": "rules-row", **payload}

        _PromptClient.patches = []
        _PromptClient.posts = []
        monkeypatch.setattr("main.get_active_prompt_version", fake_get_active_prompt_version)
        monkeypatch.setattr("main.get_user_singleton_row", fake_get_user_singleton_row)
        monkeypatch.setattr("main.upsert_user_singleton_row", fake_upsert_user_singleton_row)
        monkeypatch.setattr("main.httpx.AsyncClient", _PromptClient)

        result = asyncio.run(refine_prompt(
            RefinePromptPayload(
                instruction=self.production_instruction,
                apply=True,
                prompt_proposed=proposed_prompt,
            ),
            user_id="user-123",
        ))

        assert result["success"] is True
        assert result["applied"] is True
        assert result["prompt_version_id"] == "prompt-version-preview"
        assert upserts[0][0].endswith("/agent_sales_rules")
        created_prompt = _PromptClient.posts[0]["kwargs"]["json"]
        assert created_prompt["content"] == proposed_prompt
        assert any(item["type"] == "add" and "Règle preview validée" in item["line"] for item in result["diff"])


class TestNounesBetaReadinessControls:
    def test_cost_estimator_is_deterministic_and_positive(self):
        assert estimate_token_count("abcd") == 1
        assert estimate_token_count("abcde") == 2
        assert estimate_claude_cost_eur("hello", "bonjour") > 0

    def test_beta_account_settings_normalize_cap_windows_and_followups(self):
        settings = normalize_beta_account_settings(row={
            "ai_cost_cap_eur": "50.00",
            "ai_cost_guardrail_enabled": True,
            "allowed_send_start": "08:00",
            "allowed_send_end": "22:00",
            "min_auto_delay_seconds": 30,
            "random_auto_delay_seconds": 90,
            "follow_up_config": [
                {"stage": "j2", "delay_hours": 48, "mode": "manual"},
                {"stage": "auto_23h", "delay_hours": 23, "mode": "auto"},
            ],
        })

        assert settings["cap_eur"] == 50.0
        assert settings["allowed_send_start"] == "08:00"
        assert settings["allowed_send_end"] == "22:00"
        assert settings["min_auto_delay_seconds"] == 30
        assert settings["random_auto_delay_seconds"] == 90
        assert [item["stage"] for item in settings["follow_up_config"]] == ["auto_23h", "j2"]

    def test_allowed_send_window_blocks_after_hours_and_returns_next_window(self):
        settings = normalize_beta_account_settings(row={"allowed_send_start": "08:00", "allowed_send_end": "22:00"})
        assert is_within_allowed_send_window(datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc), settings) is True
        after_hours = datetime(2026, 8, 20, 23, 15, tzinfo=timezone.utc)
        assert is_within_allowed_send_window(after_hours, settings) is False
        assert next_allowed_send_at(after_hours, settings).isoformat() == "2026-08-21T08:00:00+00:00"

    def test_configurable_follow_up_stage_uses_account_delays(self):
        settings = normalize_beta_account_settings(row={
            "follow_up_config": [
                {"stage": "h12", "delay_hours": 12, "mode": "auto"},
                {"stage": "j2", "delay_hours": 48, "mode": "manual"},
            ]
        })

        h12 = configured_follow_up_stage(13, settings)
        j2 = configured_follow_up_stage(49, settings)
        assert h12 and h12["stage"] == "h12"
        assert j2 and j2["stage"] == "j2"

    def test_cost_cap_raises_when_spend_reaches_cap(self):
        async def fake_settings(user_id):
            return {"cap_eur": 50.0, "enabled": True}

        async def fake_spend(user_id):
            return 50.0

        with patch("main.get_beta_cost_settings", fake_settings), patch("main.get_estimated_ai_spend_eur", fake_spend):
            with pytest.raises(CostCapExceededError):
                asyncio.run(enforce_ai_cost_cap("user-123"))

    def test_bulk_auto_only_patches_supervised_and_counts_off_disabled_paused(self):
        class _Response:
            def __init__(self, payload=None):
                self.status_code = 200
                self.text = ""
                self._payload = payload or []

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _BulkClient:
            patches = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, *args, **kwargs):
                return _Response([
                    {"id": "supervised-1", "automation_mode": "supervised"},
                    {"id": "auto-1", "automation_mode": "auto"},
                    {"id": "disabled-1", "automation_mode": "disabled"},
                    {"id": "off-1", "automation_mode": "off"},
                    {"id": "paused-1", "automation_mode": "paused"},
                ])

            async def patch(self, *args, **kwargs):
                self.__class__.patches.append({"args": args, "kwargs": kwargs})
                return _Response()

        _BulkClient.patches = []
        with patch("main.httpx.AsyncClient", _BulkClient):
            result = asyncio.run(bulk_update_automation_mode(BulkAutomationModePayload(), user_id="user-123"))

        assert result["switched_to_auto"] == 1
        assert result["skipped_off_disabled"] == 3
        assert result["skipped_other"] == 1
        assert result["failed"] == 0
        assert len(_BulkClient.patches) == 1
        assert _BulkClient.patches[0]["kwargs"]["params"]["id"] == "eq.supervised-1"
        assert _BulkClient.patches[0]["kwargs"]["params"]["automation_mode"] == "eq.supervised"


class TestAngellosConversationSimulator:
    def test_includes_eight_required_scenarios(self):
        scenario_ids = {scenario["id"] for scenario in SIMULATOR_SCENARIOS}
        assert scenario_ids == {
            "skeptical-ai",
            "interested-vague",
            "price-objection",
            "send-info",
            "ghost-after-reply",
            "not-qualified",
            "hot-prospect",
            "cold-negative",
        }

    def test_deterministic_simulator_uses_current_canned_reply_for_price(self):
        scenario = next(item for item in SIMULATOR_SCENARIOS if item["id"] == "price-objection")
        reply, source = deterministic_simulator_reply(scenario)

        assert source == "current_canned_logic"
        assert "free for 30 days" in reply

    def test_score_flags_long_robotic_pitch(self):
        scenario = next(item for item in SIMULATOR_SCENARIOS if item["id"] == "not-qualified")
        result = score_simulated_reply(
            scenario,
            "Absolutely! Great question! I understand your concern. Let's book a quick call so we can discuss the paid version and move forward immediately with your acquisition process.",
        )

        assert result["quality_score"] < 80
        assert result["recommendation"] in {"retry", "human_review"}
        assert result["flags"]["trop_ia"] is True
        assert result["flags"]["pitch_premature"] is True

    def test_run_simulator_scenario_returns_transcript_score_and_recommendation(self):
        scenario = next(item for item in SIMULATOR_SCENARIOS if item["id"] == "cold-negative")
        result = asyncio.run(run_simulator_scenario(scenario, "user-123", use_ai=False))

        assert result["scenario_id"] == "cold-negative"
        assert result["angellos_reply"] == "No worries, appreciate you getting back to me."
        assert result["response_source"] == "current_canned_logic"
        assert isinstance(result["quality_score"], int)
        assert result["recommendation"] in {"pass", "retry", "human_review"}
        assert set(result["quality_judge"]["scores"].keys()) == set(QUALITY_JUDGE_SCORE_KEYS)
        assert result["transcript"][-1]["role"] == "assistant"

    def test_quality_judge_structure_is_returned_for_all_eight_scenarios(self):
        results = [
            asyncio.run(run_simulator_scenario(scenario, "user-123", use_ai=False))
            for scenario in SIMULATOR_SCENARIOS
        ]

        assert len(results) == 8
        for result in results:
            judge = result["quality_judge"]
            assert set(judge.keys()) == {"overall_score", "decision", "scores", "why", "suggested_rewrite"}
            assert 0 <= judge["overall_score"] <= 100
            assert judge["decision"] in QUALITY_JUDGE_DECISIONS
            assert set(judge["scores"].keys()) == set(QUALITY_JUDGE_SCORE_KEYS)
            assert all(1 <= score <= 10 for score in judge["scores"].values())
            assert isinstance(judge["why"], str) and judge["why"]
            assert isinstance(judge["suggested_rewrite"], str) and judge["suggested_rewrite"]

    def test_quality_judge_escalates_bad_business_reply(self):
        scenario = next(item for item in SIMULATOR_SCENARIOS if item["id"] == "skeptical-ai")
        scoring = score_simulated_reply(scenario, "Absolutely, let’s book a quick call and discuss the paid version.")
        judge = judge_simulated_reply_quality(scenario, "Absolutely, let’s book a quick call and discuss the paid version.", scoring["flags"])

        assert judge["decision"] == "human_review"
        assert judge["scores"]["risque_business"] <= 4
        assert "automated" in judge["suggested_rewrite"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
