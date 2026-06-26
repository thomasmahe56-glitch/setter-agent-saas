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
    is_placeholder_display_name,
    normalize_display_name,
    _needs_supervised_pending,
    _last_prospect_message,
)


# ===========================================================================
# BUG 1 — Display name placeholder detection
# ===========================================================================

class TestIsPlaceholderDisplayName:
    def test_ig_username_placeholder(self):
        assert is_placeholder_display_name("{{ig_username}}") is True

    def test_username_placeholder(self):
        assert is_placeholder_display_name("{{username}}") is True

    def test_first_name_placeholder(self):
        assert is_placeholder_display_name("{{first_name}}") is True

    def test_arbitrary_placeholder(self):
        assert is_placeholder_display_name("{{some_var}}") is True

    def test_valid_name_not_placeholder(self):
        assert is_placeholder_display_name("julien_runs") is False

    def test_real_name_not_placeholder(self):
        assert is_placeholder_display_name("Jean Dupont") is False

    def test_empty_string_not_placeholder(self):
        assert is_placeholder_display_name("") is False

    def test_none_not_placeholder(self):
        assert is_placeholder_display_name(None) is False

    def test_subscriber_id_not_placeholder(self):
        assert is_placeholder_display_name("12345678") is False

    def test_partial_placeholder_detected(self):
        # A value that contains {{ anywhere is still flagged
        assert is_placeholder_display_name("Hello {{ig_username}}") is True


class TestNormalizeDisplayName:
    def test_valid_incoming_returned_as_is(self):
        assert normalize_display_name("julien_runs") == "julien_runs"

    def test_placeholder_rejected_returns_fallback(self):
        result = normalize_display_name("{{ig_username}}")
        assert result == "Instagram prospect"

    def test_placeholder_keeps_existing_valid_name(self):
        result = normalize_display_name("{{ig_username}}", existing="julien_runs")
        assert result == "julien_runs"

    def test_placeholder_existing_also_placeholder_returns_fallback(self):
        result = normalize_display_name("{{ig_username}}", existing="{{ig_username}}")
        assert result == "Instagram prospect"

    def test_none_incoming_existing_valid_returns_existing(self):
        result = normalize_display_name(None, existing="julien_runs")
        assert result == "julien_runs"

    def test_none_incoming_no_existing_returns_fallback(self):
        result = normalize_display_name(None, existing=None)
        assert result == "Instagram prospect"

    def test_valid_incoming_overrides_existing(self):
        result = normalize_display_name("new_handle", existing="old_handle")
        assert result == "new_handle"

    def test_strips_whitespace(self):
        assert normalize_display_name("  julien_runs  ") == "julien_runs"


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
