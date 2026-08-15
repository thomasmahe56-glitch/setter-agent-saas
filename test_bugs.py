"""Focused regression tests for BUG 1 (placeholder display names) and BUG 2 (supervised activation)."""
import asyncio
import types
import pytest
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
