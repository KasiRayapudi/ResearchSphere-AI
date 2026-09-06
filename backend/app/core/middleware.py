"""
FastAPI Middleware Stack for ResearchSphere AI:
- RequestIDMiddleware: UUID4 correlation ID on every request
- LoggingMiddleware: Structured JSON request/response logging
- SecurityHeadersMiddleware: HSTS, CSP, X-Frame-Options
- RateLimitingMiddleware: In-memory token-bucket rate limiter
"""

import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings
from app.core.logging import get_logger, request_id_var
from app.core.redis_client import get_redis, mark_unavailable

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
        response.headers["Cross-Origin-Resource-Policy"] = (
            settings.HEADER_CROSS_ORIGIN_RESOURCE_POLICY
        )
        response.headers["Cross-Origin-Opener-Policy"] = settings.HEADER_CROSS_ORIGIN_OPENER_POLICY

        # COEP is opt-in: require-corp breaks any cross-origin resource that
        # does not opt in (including the docs CDN), so it stays off by default.
        if settings.HEADER_CROSS_ORIGIN_EMBEDDER_POLICY:
            response.headers["Cross-Origin-Embedder-Policy"] = (
                settings.HEADER_CROSS_ORIGIN_EMBEDDER_POLICY
            )

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
    """Distributed rate limiter with an in-memory fallback.

    Redis holds a sliding-window counter per client and tier, so the limit is
    shared across every backend instance. The window uses a sorted set updated
    inside a MULTI/EXEC pipeline, which keeps the read-modify-write atomic
    without needing a Lua script.

    Degradation: when Redis is unavailable the limiter falls back to the
    original per-process token bucket rather than failing the request. That is
    weaker (each worker counts separately) but a Redis outage must not take the
    API down. The fallback bucket store is bounded and swept, so it cannot grow
    without limit the way the previous implementation could.

    The constructor signature is unchanged, so registration in main.py did not
    need to change.
    """

    #: Cap on distinct in-memory buckets before the oldest are evicted.
    _MAX_LOCAL_BUCKETS = 10_000

    def __init__(self, app, default_rpm: int = 100, auth_rpm: int = 20):
        super().__init__(app)
        self.default_rpm = default_rpm
        self.auth_rpm = auth_rpm
        self._buckets: dict = {}
        self._last_sweep = time.time()

    # -- local fallback ---------------------------------------------------
    def _sweep(self, now: float) -> None:
        """Evict idle buckets so the fallback store stays bounded."""
        if now - self._last_sweep < 60:
            return
        self._last_sweep = now
        stale = [k for k, b in self._buckets.items() if now - b["last_refill"] > 300]
        for key in stale:
            self._buckets.pop(key, None)
        if len(self._buckets) > self._MAX_LOCAL_BUCKETS:
            for key in sorted(self._buckets, key=lambda k: self._buckets[k]["last_refill"])[
                : len(self._buckets) - self._MAX_LOCAL_BUCKETS
            ]:
                self._buckets.pop(key, None)

    def _check_local(self, key: str, max_rpm: int):
        now = time.time()
        self._sweep(now)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = {"tokens": float(max_rpm), "last_refill": now}
            self._buckets[key] = bucket
        elapsed = now - bucket["last_refill"]
        bucket["tokens"] = min(max_rpm, bucket["tokens"] + elapsed * (max_rpm / 60.0))
        bucket["last_refill"] = now
        if bucket["tokens"] < 1:
            return False, 0, 60
        bucket["tokens"] -= 1
        return True, int(bucket["tokens"]), 60

    # -- redis ------------------------------------------------------------
    def _check_redis(self, client, key: str, max_rpm: int):
        """Sliding-window counter. Returns (allowed, remaining, retry_after)."""
        now = time.time()
        window_start = now - 60
        redis_key = f"ratelimit:{key}"
        pipe = client.pipeline()
        pipe.zremrangebyscore(redis_key, 0, window_start)
        pipe.zadd(redis_key, {f"{now}:{uuid.uuid4().hex[:8]}": now})
        pipe.zcard(redis_key)
        pipe.expire(redis_key, 120)
        results = pipe.execute()
        count = int(results[2])
        if count > max_rpm:
            # Remove the request we just recorded so a blocked caller does not
            # keep extending its own window.
            try:
                client.zremrangebyrank(redis_key, -1, -1)
            except Exception:
                pass
            return False, 0, 60
        return True, max(0, max_rpm - count), 60

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path.lower()

        # Health probes must never be rate limited: an orchestrator polling
        # them would otherwise mark a healthy instance as down.
        if path in ("/api/live", "/api/health", "/api/ready", "/live", "/health", "/ready"):
            return await call_next(request)

        is_sensitive = "/auth/" in path or "/upload" in path
        max_rpm = self.auth_rpm if is_sensitive else self.default_rpm
        bucket_key = f"{client_ip}:{max_rpm}"

        backend = "local"
        redis_conn = get_redis()
        if redis_conn is not None:
            try:
                allowed, remaining, retry_after = self._check_redis(redis_conn, bucket_key, max_rpm)
                backend = "redis"
            except Exception as exc:
                mark_unavailable(exc)
                allowed, remaining, retry_after = self._check_local(bucket_key, max_rpm)
        else:
            allowed, remaining, retry_after = self._check_local(bucket_key, max_rpm)

        if not allowed:
            logger.warning(f"Rate limit exceeded for {client_ip} on {path} (backend={backend})")
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "RATE_LIMIT_EXCEEDED",
                        "message": "Too many requests. Please try again later.",
                    }
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(max_rpm),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(max_rpm)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Backend"] = backend
        return response
