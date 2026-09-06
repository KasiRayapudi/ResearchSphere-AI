"""
FastAPI Middleware Stack for ResearchSphere AI:
- RequestIDMiddleware: UUID4 correlation ID on every request
- LoggingMiddleware: Structured JSON request/response logging
- SecurityHeadersMiddleware: HSTS, CSP, X-Frame-Options
- RateLimitingMiddleware: In-memory token-bucket rate limiter
"""
import re
import uuid
import time
import logging
from collections import defaultdict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from app.core.logging import request_id_var, get_logger

logger = get_logger("middleware")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Injects and propagates X-Request-ID across requests."""

    # Client-supplied IDs are echoed back and written to logs, so constrain them
    # to a safe charset/length to prevent log injection.
    _MAX_ID_LEN = 64
    _SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        if incoming and len(incoming) <= self._MAX_ID_LEN and self._SAFE_ID.match(incoming):
            req_id = incoming
        else:
            req_id = str(uuid.uuid4())
        request.state.request_id = req_id
        token = request_id_var.set(req_id)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = req_id
            return response
        finally:
            request_id_var.reset(token)


class LoggingMiddleware(BaseHTTPMiddleware):
    """Logs method, path, client IP, status code, and duration in ms."""

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        client_ip = request.client.host if request.client else "unknown"

        response = await call_next(request)

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        req_id = getattr(request.state, "request_id", "")

        # Pass the ID explicitly: this middleware runs outside RequestIDMiddleware,
        # so the request_id contextvar has already been reset by this point.
        logger.info(
            f"{request.method} {request.url.path} -> {response.status_code} ({duration_ms}ms)",
            extra={
                "duration_ms": duration_ms,
                "status_code": response.status_code,
                "request_id": req_id,
                "client_ip": client_ip,
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Injects production security headers."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # HSTS - uncomment when HTTPS is configured
        # response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class RateLimitingMiddleware(BaseHTTPMiddleware):
    """
    Simple in-memory token-bucket rate limiter.
    Default: 100 requests/minute per IP. Auth/Upload: 20 requests/minute.
    """

    def __init__(self, app, default_rpm: int = 100, auth_rpm: int = 20):
        super().__init__(app)
        self.default_rpm = default_rpm
        self.auth_rpm = auth_rpm
        self._buckets: dict = defaultdict(lambda: {"tokens": default_rpm, "last_refill": time.time()})

    def _refill(self, bucket: dict, max_tokens: int):
        now = time.time()
        elapsed = now - bucket["last_refill"]
        refill = elapsed * (max_tokens / 60.0)
        bucket["tokens"] = min(max_tokens, bucket["tokens"] + refill)
        bucket["last_refill"] = now

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path.lower()

        # Determine rate limit tier
        is_sensitive = "/auth/" in path or "/upload" in path
        max_rpm = self.auth_rpm if is_sensitive else self.default_rpm

        bucket_key = f"{client_ip}:{max_rpm}"
        bucket = self._buckets[bucket_key]
        self._refill(bucket, max_rpm)

        if bucket["tokens"] < 1:
            logger.warning(f"Rate limit exceeded for {client_ip} on {path}")
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMIT_EXCEEDED",
                        "message": "Too many requests. Please try again later.",
                    }
                },
            )

        bucket["tokens"] -= 1
        response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(int(bucket["tokens"]))
        return response
