from __future__ import annotations

import hashlib
import time
from collections import defaultdict

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


DEFAULT_BODY_LIMIT = 1_000_000
UPLOAD_BODY_LIMIT = 12_000_000


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = UPLOAD_BODY_LIMIT if scope.get("path") == "/agent/knowledge/extract" else DEFAULT_BODY_LIMIT
        content_length = Headers(scope=scope).get("content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    await JSONResponse({"detail": "Request body is too large"}, status_code=413)(scope, receive, send)
                    return
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await JSONResponse({"detail": "Request body is too large"}, status_code=413)(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        async def secure_send(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                ])
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, secure_send)


class RateLimitMiddleware:
    """Bound anonymous amplification and expensive endpoint frequency.

    Both an IP bucket and a credential bucket are enforced. Hashes are used so
    bearer tokens and webhook secrets are never retained in memory or logs.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.history: dict[str, list[float]] = defaultdict(list)
        self.request_count = 0
        self.max_keys = 20_000

    @staticmethod
    def _client_ip(scope) -> str:
        real_ip = Headers(scope=scope).get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
        client = scope.get("client")
        return str(client[0]) if client else "unknown"

    @staticmethod
    def _route_limit(path: str, method: str) -> tuple[int, int]:
        if method in {"OPTIONS", "HEAD"}:
            return 120, 60
        if path in {"/webhook", "/webhooks/whatsapp"}:
            return 30, 60
        if path.startswith(("/playground", "/simulator", "/refine-prompt", "/agent/avatar/generate", "/agent/sales-rules/generate", "/agent/knowledge/extract")):
            return 10, 60
        return 120, 60

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path = scope.get("path", "")
        method = scope.get("method", "GET")
        limit, window = self._route_limit(path, method)
        ip_key = f"ip:{self._client_ip(scope)}:{method}:{path}"
        credential = headers.get("authorization") or headers.get("x-webhook-secret") or ""
        credential_key = f"credential:{hashlib.sha256(credential.encode()).hexdigest()[:24]}:{method}:{path}" if credential else None
        keys = [ip_key, *([credential_key] if credential_key else [])]
        now = time.monotonic()

        self.request_count += 1
        if self.request_count % 100 == 0:
            stale = [key for key, timestamps in self.history.items() if not timestamps or now - timestamps[-1] >= 60]
            for key in stale:
                self.history.pop(key, None)
        if any(key not in self.history for key in keys) and len(self.history) >= self.max_keys:
            await JSONResponse({"detail": "Rate limiter capacity reached"}, status_code=503, headers={"Retry-After": str(window)})(scope, receive, send)
            return

        for key in keys:
            self.history[key] = [timestamp for timestamp in self.history[key] if now - timestamp < window]
            if len(self.history[key]) >= limit:
                await JSONResponse({"detail": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": str(window)})(scope, receive, send)
                return
        for key in keys:
            self.history[key].append(now)
        await self.app(scope, receive, send)
