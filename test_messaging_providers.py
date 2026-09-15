import base64
import os
from datetime import datetime, timedelta, timezone

import pytest

from messaging_providers import (
    META_PROVIDER,
    can_send_meta_message,
    decrypt_secret,
    encrypt_secret,
    is_explicit_opt_out,
    parse_meta_instagram_webhook,
)


class _Response:
    def __init__(self, data=None, status_code=200):
        self._data = [] if data is None else data
        self.status_code = status_code
        self.text = "" if data is None else "json"

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_token_envelope_round_trip_and_random_nonce():
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    first = encrypt_secret("sensitive-token", key)
    second = encrypt_secret("sensitive-token", key)
    assert first.startswith("v1:")
    assert first != second
    assert "sensitive-token" not in first
    assert decrypt_secret(first, key) == "sensitive-token"


def test_token_envelope_rejects_wrong_key():
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    wrong = base64.urlsafe_b64encode(os.urandom(32)).decode()
    with pytest.raises(Exception):
        decrypt_secret(encrypt_secret("token", key), wrong)


def test_meta_webhook_normalizes_text_attachment_and_ignores_echo():
    payload = {
        "object": "instagram",
        "entry": [{
            "id": "instagram-a",
            "messaging": [
                {
                    "sender": {"id": "prospect-a"},
                    "recipient": {"id": "instagram-a"},
                    "timestamp": 1_700_000_000_000,
                    "message": {"mid": "message-1", "text": "Hello"},
                },
                {
                    "sender": {"id": "prospect-a"},
                    "timestamp": 1_700_000_001_000,
                    "message": {"mid": "message-2", "attachments": [{"type": "share", "payload": {"url": "https://example.test/post"}}]},
                },
                {
                    "sender": {"id": "instagram-a"},
                    "message": {"mid": "echo", "text": "sent", "is_echo": True},
                },
            ],
        }],
    }
    events = parse_meta_instagram_webhook(payload)
    assert [event.external_event_id for event in events] == ["message-1", "message-2"]
    assert all(event.provider == META_PROVIDER and event.account_id == "instagram-a" for event in events)
    assert events[1].attachments[0]["type"] == "shared_post_or_reel"
    assert events[1].text == "[Instagram attachment: shared_post_or_reel]"


def _connection(**patch):
    return {
        "status": "connected",
        "scopes": ["instagram_business_basic", "instagram_business_manage_messages"],
        **patch,
    }


def _conversation(**patch):
    return {
        "last_inbound_at": datetime.now(timezone.utc).isoformat(),
        "automation_mode": "auto",
        "agent_active": True,
        "human_takeover": False,
        "contact_status": "active",
        **patch,
    }


@pytest.mark.parametrize("conversation,reason", [
    (_conversation(contact_status="opted_out"), "recipient_opted_out"),
    (_conversation(human_takeover=True), "human_takeover"),
    (_conversation(automation_mode="disabled"), "automation_disabled"),
    (_conversation(last_inbound_at=None), "no_user_initiated_conversation"),
    (_conversation(last_inbound_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()), "outside_standard_messaging_window"),
])
def test_meta_send_eligibility_blocks_unsafe_states(conversation, reason):
    assert can_send_meta_message(connection=_connection(), conversation=conversation) == (False, reason)


def test_meta_send_eligibility_requires_minimal_permissions():
    assert can_send_meta_message(
        connection=_connection(scopes=["instagram_business_basic"]),
        conversation=_conversation(),
    ) == (False, "missing_permission")


def test_meta_send_eligibility_accepts_recent_user_initiated_conversation():
    assert can_send_meta_message(connection=_connection(), conversation=_conversation()) == (True, "eligible")


@pytest.mark.parametrize("body,expected_status", [
    ('{"recipient_id":"meta-recipient","message_id":"message-1"}', 200),
    ('{"message_id":"message-1","recipient_id":"different-person"}', 503),
    ('{"status":"error"}', 503),
    ('{}', 503),
    ('not-json', 503),
])
def test_follow_up_meta_send_requires_delivery_receipt_before_counting_success(
    monkeypatch, capsys, body, expected_status,
):
    import asyncio
    from unittest.mock import AsyncMock
    import main

    connection = {"id": "connection-1", "status": "connected", "user_id": "tenant-1",
                  "external_account_id": "account-1",
                  "scopes": ["instagram_business_basic", "instagram_business_manage_messages"]}
    conversation = {"id": "conversation-1", "user_id": "tenant-1", "channel": "instagram",
                    "messaging_provider": META_PROVIDER, "messaging_connection_id": "connection-1",
                    "external_contact_id": "meta-recipient", "automation_mode": "auto",
                    "agent_active": True, "last_inbound_at": datetime.now(timezone.utc).isoformat()}
    provider_send = AsyncMock(return_value={"status_code": 200, "body": body})
    usage = AsyncMock()
    monkeypatch.setattr(main.config, "meta_instagram_enabled", True)
    monkeypatch.setattr(main.config, "meta_instagram_send_enabled", True)
    monkeypatch.setattr(main, "get_messaging_connection", AsyncMock(return_value=connection))
    monkeypatch.setattr(main, "get_valid_access_token", AsyncMock(return_value="synthetic-token"))
    monkeypatch.setattr(main.META_INSTAGRAM_PROVIDER, "send_message", provider_send)
    monkeypatch.setattr(main, "record_usage_ledger_event", usage)

    result = asyncio.run(main.send_channel_message(conversation, "Synthetic follow-up", at_most_once=True))
    assert result["status_code"] == expected_status
    assert provider_send.await_args.kwargs["max_attempts"] == 1
    if expected_status == 200:
        usage.assert_awaited_once()
        assert "meta.message.sent" in capsys.readouterr().out
    else:
        assert "meta_delivery_unverified" in result["body"]
        usage.assert_not_awaited()
        assert "meta.message.unverified" in capsys.readouterr().out


@pytest.mark.parametrize("message", [
    "STOP", "unsubscribe please", "Don't contact me again", "ne me contacte plus", "arrête",
])
def test_explicit_opt_out_is_deterministic(message):
    assert is_explicit_opt_out(message)


def test_account_id_stays_provider_owned_for_tenant_matching():
    payload_a = {"object": "instagram", "entry": [{"id": "account-a", "messaging": [{"sender": {"id": "same-prospect"}, "message": {"mid": "a-1", "text": "A"}}]}]}
    payload_b = {"object": "instagram", "entry": [{"id": "account-b", "messaging": [{"sender": {"id": "same-prospect"}, "message": {"mid": "b-1", "text": "B"}}]}]}
    assert parse_meta_instagram_webhook(payload_a)[0].account_id == "account-a"
    assert parse_meta_instagram_webhook(payload_b)[0].account_id == "account-b"


def test_oauth_start_uses_server_owned_tenant_and_minimal_scopes(monkeypatch):
    import asyncio
    import main

    captured = {}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, url, **kwargs):
            captured.update(kwargs["json"])
            return _Response()

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    monkeypatch.setattr(main.config, "meta_instagram_enabled", True)
    monkeypatch.setattr(main.config, "meta_instagram_oauth_enabled", True)
    monkeypatch.setattr(main.config, "meta_app_id", "app-id")
    monkeypatch.setattr(main.config, "meta_instagram_redirect_uri", "https://backend.test/oauth/meta/instagram/callback")
    monkeypatch.setattr(main.config, "messaging_token_encryption_key", base64.urlsafe_b64encode(os.urandom(32)).decode())
    result = asyncio.run(main.start_instagram_oauth(user_id="tenant-a"))
    assert captured["user_id"] == "tenant-a"
    assert "tenant_id" not in result["authorization_url"]
    assert "user_id" not in result["authorization_url"]
    assert "instagram_business_basic" in result["authorization_url"]
    assert "instagram_business_manage_messages" in result["authorization_url"]


@pytest.mark.parametrize("rows", [[], None])
def test_oauth_state_rejects_invalid_expired_or_reused_rows(monkeypatch, rows):
    import asyncio
    import main
    from fastapi import HTTPException

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def patch(self, *args, **kwargs): return _Response(rows)

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.consume_instagram_oauth_state("invalid-or-expired"))
    assert caught.value.status_code == 400


def test_connection_status_never_exposes_token():
    import main
    public = main.sanitized_connection({
        "id": "connection-a",
        "provider": META_PROVIDER,
        "status": "connected",
        "external_username": "account",
        "access_token_encrypted": "v1:secret-envelope",
    })
    assert public["connected"] is True
    assert "access_token_encrypted" not in public


def test_oauth_exchange_rejects_missing_message_permission(monkeypatch):
    import asyncio
    import main
    from fastapi import HTTPException

    responses = iter([
        _Response({"access_token": "short-token", "user_id": "account"}),
        _Response({"access_token": "long-token", "expires_in": 3600}),
        _Response({"user_id": "account", "username": "business", "account_type": "BUSINESS"}),
        _Response({"error": {"code": 10}}, status_code=400),
    ])
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, *args, **kwargs): return next(responses)
        async def get(self, *args, **kwargs): return next(responses)

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    monkeypatch.setattr(main.config, "meta_app_id", "app")
    monkeypatch.setattr(main.config, "meta_app_secret", "secret")
    monkeypatch.setattr(main.config, "meta_instagram_redirect_uri", "https://backend.test/callback")
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.exchange_instagram_oauth_code("code"))
    assert caught.value.status_code == 400
    assert "permissions" in str(caught.value.detail).lower()


def test_oauth_exchange_accepts_instagram_login_token_with_message_access(monkeypatch):
    import asyncio
    import main

    responses = iter([
        _Response({"access_token": "short-token", "user_id": "account"}),
        _Response({"access_token": "long-token", "expires_in": 3600}),
        _Response({"user_id": "account", "username": "business", "account_type": "BUSINESS"}),
        _Response({"data": []}),
    ])

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, *args, **kwargs): return next(responses)
        async def get(self, *args, **kwargs): return next(responses)

    monkeypatch.setattr(main.httpx, "AsyncClient", Client)
    monkeypatch.setattr(main.config, "meta_app_id", "app")
    monkeypatch.setattr(main.config, "meta_app_secret", "secret")
    monkeypatch.setattr(main.config, "meta_instagram_redirect_uri", "https://backend.test/callback")

    result = asyncio.run(main.exchange_instagram_oauth_code("code"))

    assert result["external_account_id"] == "account"
    assert result["external_username"] == "business"
    assert set(result["scopes"]) == main.REQUIRED_META_SCOPES


def test_meta_webhook_routes_each_account_to_its_own_tenant(monkeypatch):
    import asyncio
    import hashlib
    import hmac
    import json
    from unittest.mock import AsyncMock
    import main

    payload = {"object": "instagram", "entry": [
        {"id": "account-a", "messaging": [{"sender": {"id": "prospect-a"}, "message": {"mid": "event-a", "text": "A"}}]},
        {"id": "account-b", "messaging": [{"sender": {"id": "prospect-b"}, "message": {"mid": "event-b", "text": "B"}}]},
    ]}
    body = json.dumps(payload).encode()

    class Request:
        async def body(self): return body

    connections = {
        "account-a": {"id": "connection-a", "user_id": "tenant-a", "status": "connected"},
        "account-b": {"id": "connection-b", "user_id": "tenant-b", "status": "connected"},
    }
    async def get_connection(**kwargs): return connections.get(kwargs.get("external_account_id"))

    handled = []
    async def handle(**kwargs):
        handled.append((kwargs["user_id"], kwargs["external_contact_id"], kwargs["transport_metadata"]["account_id"]))
        return {"conversation_id": "conversation"}

    monkeypatch.setattr(main.config, "meta_instagram_enabled", True)
    monkeypatch.setattr(main.config, "meta_instagram_webhook_enabled", True)
    monkeypatch.setattr(main, "META_APP_SECRET", "app-secret")
    monkeypatch.setattr(main, "get_messaging_connection", get_connection)
    monkeypatch.setattr(main, "get_active_messaging_provider", AsyncMock(return_value=META_PROVIDER))
    monkeypatch.setattr(main, "reserve_inbound_event", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "finish_inbound_event", AsyncMock())
    monkeypatch.setattr(main, "patch_messaging_connection", AsyncMock())
    monkeypatch.setattr(main, "handle_inbound_message", handle)
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    response = asyncio.run(main.instagram_webhook(Request(), x_hub_signature_256=signature))
    assert response["processed"] == 2
    assert handled == [
        ("tenant-a", "prospect-a", "account-a"),
        ("tenant-b", "prospect-b", "account-b"),
    ]


def test_duplicate_meta_webhook_never_enters_setter_twice(monkeypatch):
    import asyncio
    import hashlib
    import hmac
    import json
    from unittest.mock import AsyncMock
    import main

    event = {"sender": {"id": "prospect"}, "message": {"mid": "same-event", "text": "Hello"}}
    body = json.dumps({"object": "instagram", "entry": [{"id": "account", "messaging": [event, event]}]}).encode()
    class Request:
        async def body(self): return body
    async def connection(**kwargs): return {"id": "connection", "user_id": "tenant", "status": "connected"}
    reservations = iter([True, False])
    async def reserve(*args, **kwargs): return next(reservations)

    handle = AsyncMock(return_value={"conversation_id": "conversation"})
    monkeypatch.setattr(main.config, "meta_instagram_enabled", True)
    monkeypatch.setattr(main.config, "meta_instagram_webhook_enabled", True)
    monkeypatch.setattr(main, "META_APP_SECRET", "app-secret")
    monkeypatch.setattr(main, "get_messaging_connection", connection)
    monkeypatch.setattr(main, "get_active_messaging_provider", AsyncMock(return_value=META_PROVIDER))
    monkeypatch.setattr(main, "reserve_inbound_event", reserve)
    monkeypatch.setattr(main, "finish_inbound_event", AsyncMock())
    monkeypatch.setattr(main, "patch_messaging_connection", AsyncMock())
    monkeypatch.setattr(main, "handle_inbound_message", handle)
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    response = asyncio.run(main.instagram_webhook(Request(), x_hub_signature_256=signature))
    assert response["processed"] == 1
    assert handle.await_count == 1


def test_meta_profile_resolution_prefers_username_and_keeps_token_out_of_url(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    import main

    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"name": "Prospect Name", "username": "prospect.username"}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False

        async def get(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(main, "get_valid_access_token", AsyncMock(return_value="server-only-token"))
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)

    result = asyncio.run(main.resolve_meta_instagram_contact_name({"id": "connection"}, "123456"))

    assert result == "prospect.username"
    assert captured["url"].endswith("/123456")
    assert "server-only-token" not in captured["url"]
    assert captured["headers"] == {"Authorization": "Bearer server-only-token"}
    assert captured["params"] == {"fields": "name,username"}


def test_meta_profile_resolution_is_best_effort(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    import main

    class Response:
        status_code = 403

        def json(self):
            return {"error": {"message": "not available"}}

    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def get(self, *args, **kwargs): return Response()

    monkeypatch.setattr(main, "get_valid_access_token", AsyncMock(return_value="server-only-token"))
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)

    assert asyncio.run(main.resolve_meta_instagram_contact_name({"id": "connection"}, "123456")) is None


def test_mocked_meta_e2e_webhook_core_ai_and_send(monkeypatch):
    import asyncio
    import hashlib
    import hmac
    import json
    from unittest.mock import AsyncMock
    import main
    from ai_generation import AiGenerationResult

    body = json.dumps({
        "object": "instagram",
        "entry": [{"id": "account", "messaging": [{
            "sender": {"id": "prospect"},
            "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "message": {"mid": "event", "text": "Tell me more"},
        }]}],
    }).encode()
    class Request:
        async def body(self): return body
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def patch(self, *args, **kwargs): return _Response()

    connection = {
        "id": "connection", "user_id": "tenant", "status": "connected",
        "external_account_id": "account",
        "scopes": ["instagram_business_basic", "instagram_business_manage_messages"],
    }
    conversation = {
        "id": "conversation", "user_id": "tenant", "channel": "instagram",
        "external_contact_id": "prospect", "username": "prospect", "display_name": "prospect",
        "messaging_provider": META_PROVIDER, "messaging_connection_id": "connection",
        "automation_mode": "auto", "agent_active": True, "human_takeover": False,
        "contact_status": "active", "history": [], "last_inbound_at": datetime.now(timezone.utc).isoformat(),
    }
    async def get_connection(**kwargs): return connection
    provider_send = AsyncMock(return_value={"status_code": 200, "body": '{"message_id":"outbound"}'})

    monkeypatch.setattr(main.config, "meta_instagram_enabled", True)
    monkeypatch.setattr(main.config, "meta_instagram_webhook_enabled", True)
    monkeypatch.setattr(main.config, "meta_instagram_send_enabled", True)
    monkeypatch.setattr(main, "META_APP_SECRET", "app-secret")
    monkeypatch.setattr(main, "get_messaging_connection", get_connection)
    monkeypatch.setattr(main, "get_active_messaging_provider", AsyncMock(return_value=META_PROVIDER))
    monkeypatch.setattr(main, "get_contact_by_external_id", AsyncMock(return_value=conversation))
    monkeypatch.setattr(main, "get_active_prompt", AsyncMock(return_value="Tenant setter prompt"))
    monkeypatch.setattr(main, "get_beta_cost_settings", AsyncMock(return_value={"allowed_send_start": "00:00", "allowed_send_end": "23:59"}))
    monkeypatch.setattr(main, "enforce_ai_cost_cap", AsyncMock())
    monkeypatch.setattr(main, "record_ai_usage_event", AsyncMock())
    monkeypatch.setattr(main, "record_usage_ledger_event", AsyncMock())
    monkeypatch.setattr(main, "generate_ai_generation", lambda *args, **kwargs: AiGenerationResult(text="AI reply", usage=None))
    monkeypatch.setattr(main, "get_valid_access_token", AsyncMock(return_value="server-only-token"))
    monkeypatch.setattr(main.META_INSTAGRAM_PROVIDER, "send_message", provider_send)
    monkeypatch.setattr(main, "reserve_inbound_event", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "finish_inbound_event", AsyncMock())
    monkeypatch.setattr(main, "patch_messaging_connection", AsyncMock())
    monkeypatch.setattr(main.httpx, "AsyncClient", Client)

    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    response = asyncio.run(main.instagram_webhook(Request(), x_hub_signature_256=signature))
    assert response == {"success": True, "processed": 1}
    provider_send.assert_awaited_once_with(
        account_id="account", recipient_id="prospect", text="AI reply", access_token="server-only-token",
    )
