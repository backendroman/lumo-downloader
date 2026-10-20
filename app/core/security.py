import json
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.services.errors import error_message

SESSION_COOKIE = "lumo_sid"
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def session_from_request(request: Request) -> str | None:
    value = request.cookies.get(SESSION_COOKIE)
    return value if value and SESSION_RE.match(value) else None


def client_ip(request: Request) -> str:
    # behind nginx this relies on uvicorn --proxy-headers
    return request.client.host if request.client else "unknown"


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        self._sweep(now)
        return True

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < 300:
            return
        self._last_sweep = now
        for key in [k for k, v in self._hits.items() if not v or v[-1] < now - 3600]:
            self._hits.pop(key, None)


CSP = (
    "default-src 'self'; "
    # alpine csp build, no unsafe-eval needed
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' https: data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "media-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    b"content-security-policy": CSP.encode(),
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"no-referrer",
    b"x-frame-options": b"DENY",
    b"permissions-policy": b"camera=(), microphone=(), geolocation=(), clipboard-read=(self)",
    b"cross-origin-opener-policy": b"same-origin",
}


class SecurityMiddleware:
    # plain ASGI instead of BaseHTTPMiddleware - that one breaks SSE streaming

    def __init__(self, app: ASGIApp, max_body: int) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None:
            try:
                too_big = int(length) > self.max_body
            except ValueError:
                too_big = True
            if too_big:
                await _send_too_large(send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise _BodyTooLarge
            return message

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in SECURITY_HEADERS.items() if k not in existing]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except _BodyTooLarge:
            await _send_too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _send_too_large(send: Send) -> None:
    body = json.dumps(
        {"ok": False, "error": {"code": "payload_too_large", "message": error_message("payload_too_large")}},
        ensure_ascii=False,
    ).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
