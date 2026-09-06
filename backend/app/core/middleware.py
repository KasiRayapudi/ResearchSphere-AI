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
from app.core.config import settings

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
    """Injects production security headers.

    All values come from configuration. Two behaviours differ by environment:

    * HSTS is only sent in production. Sending it over plain HTTP in
      development pins the browser to HTTPS for localhost and locks the
      developer out until they clear the pin.
    * The docs routes get a relaxed CSP, because Swagger UI and ReDoc load
      their assets from a CDN and inline their bootstrap script. Applying the
      strict application CSP there would leave a blank page.

    ``X-XSS-Protection`` is deliberately NOT sent: it is deprecated, ignored by
    modern browsers, and its legacy auditor introduced vulnerabilities of its
    own. CSP replaces it.
    """

    #: Paths that need the relaxed CSP so the API documentation renders.
    _DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if not settings.SECURITY_HEADERS_ENABLED:
            return response

        path = request.url.path
        response.headers["X-Content-Type-Options"] = settings.HEADER_X_CONTENT_TYPE_OPTIONS
        response.headers["X-Frame-Options"] = settings.HEADER_X_FRAME_OPTIONS
        response.headers["Referrer-Policy"] = settings.HEADER_REFERRER_POLICY
        response.headers["Permissions-Policy"] = settings.HEADER_PERMISSIONS_POLICY
        response.headers["Cross-Origin-Resource-Policy"] = settings.HEADER_CROSS_ORIGIN_RESOURCE_POLICY
        response.headers["Cross-Origin-Opener-Policy"] = settings.HEADER_CROSS_ORIGIN_OPENER_POLICY

        # COEP is opt-in: require-corp breaks any cross-origin resource that
        # does not opt in (including the docs CDN), so it stays off by default.
        if settings.HEADER_CROSS_ORIGIN_EMBEDDER_POLICY:
            response.headers["Cross-Origin-Embedder-Policy"] = settings.HEADER_CROSS_ORIGIN_EMBEDDER_POLICY

        if path.startswith(self._DOCS_PATHS):
            csp = settings.CSP_DOCS
        else:
            csp = settings.CSP_DEFAULT
        if csp:
            response.headers["Content-Security-Policy"] = csp

        # HSTS: production only, and never over a plain-HTTP request.
        if settings.is_production and settings.HSTS_ENABLED:
            response.headers["Strict-Transport-Security"] = settings.hsts_value

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
