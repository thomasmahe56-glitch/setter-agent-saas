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
    WebhookPayload,
    extract_manychat_ig_username,
    handle_inbound_message,
    is_placeholder_display_name,
    is_real_instagram_username,
    normalize_display_name,
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
            return "Base prompt"

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
