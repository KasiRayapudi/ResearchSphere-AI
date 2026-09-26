import hmac
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.models  # Ensure all models are imported for metadata creation
from app.api.v1 import (
    admin_router,
    analytics_router,
    auth_router,
    chat_router,
    documents_router,
    mcp_router,
    members_router,
    reports_router,
    research_router,
    websocket_router,
    workspaces_router,
)
from app.core import metrics
from app.core.config import ConfigurationError, settings, validate_configuration
from app.core.database import engine
from app.core.exception_handlers import (
    app_exception_handler,
    generic_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)
from app.core.exceptions import AppException
from app.core.logging import get_logger, setup_logging
from app.core.middleware import (
    LoggingMiddleware,
    MetricsMiddleware,
    RateLimitingMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.schema import verify_schema
from app.realtime.broker import get_broker
from app.realtime.manager import get_manager
from app.realtime.revalidation import revalidate

# Optional external services imports for health checks
try:
    from qdrant_client import QdrantClient
except Exception:  # pragma: no cover
    QdrantClient = None
try:
    import redis
except Exception:  # pragma: no cover
    redis = None

# ---------------------------------------------------------------------------
# Logging initialization (runs once at import time)
# ---------------------------------------------------------------------------
setup_logging()
logger = get_logger("main")

# ---------------------------------------------------------------------------
# Application runtime state (populated by the lifespan startup hook)
# ---------------------------------------------------------------------------
START_TIME: datetime | None = None
qdrant_client = None
redis_client = None


# ---------------------------------------------------------------------------
# Dependency checks - shared by the lifespan hook and the health endpoints so
# startup diagnostics and probe output can never drift apart.
# ---------------------------------------------------------------------------
def check_database() -> str:
    """Verify the database accepts a trivial query."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        logger.error(f"Database check failed: {exc}")
        return "failed"


def check_qdrant() -> str:
    """Verify Qdrant is reachable. The collection is created lazily on first
    ingest, so a reachable server with no collection still counts as ``ok``."""
    if QdrantClient is None:
        return "unavailable"
    try:
        client = qdrant_client or QdrantClient(
            host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=5
        )
        client.get_collections()
        return "ok"
    except Exception as exc:
        logger.error(f"Qdrant check failed: {exc}")
        return "failed"


def check_redis() -> str:
    """Verify Redis when configured. Redis is optional: a blank REDIS_URL
    reports ``disabled`` and does not hold readiness back."""
    if not settings.redis_enabled:
        return "disabled"
    if redis is None:
        return "unavailable"
    try:
        client = redis_client or redis.from_url(settings.REDIS_URL, socket_connect_timeout=5)
        client.ping()
        return "ok"
    except Exception as exc:
        logger.error(f"Redis check failed: {exc}")
        return "failed"


def check_gemini() -> str:
    """Verify the Gemini API key is configured (no network call)."""
    return "configured" if getattr(settings, "GEMINI_API_KEY", "") else "missing"


def check_storage() -> str:
    """Verify the upload directory exists and is writable."""
    upload_path = Path(getattr(settings, "UPLOAD_DIR", "./uploads"))
    try:
        upload_path.mkdir(parents=True, exist_ok=True)
        probe = upload_path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return "ok"
    except Exception as exc:
        logger.error(f"Storage check failed: {exc}")
        return "failed"


def check_resources() -> dict:
    """Sample disk and memory, and report them against the warning thresholds.

    Returns a status map plus the raw measurements, so the health endpoints can
    show numbers rather than only a verdict.
    """
    sample = metrics.collect_process_metrics()
    detail = {"measurements": sample}

    disk_percent = sample.get("disk_usage_percent")
    if disk_percent is None:
        detail["disk"] = "unavailable"
    elif disk_percent >= settings.DISK_USAGE_WARN_PERCENT:
        detail["disk"] = "degraded"
        logger.warning(
            f"Disk usage {disk_percent:.1f}% is at or above the "
            f"{settings.DISK_USAGE_WARN_PERCENT}% threshold"
        )
    else:
        detail["disk"] = "ok"

    memory_percent = sample.get("system_memory_percent")
    if memory_percent is None:
        detail["memory"] = "unavailable"
    elif memory_percent >= settings.MEMORY_USAGE_WARN_PERCENT:
        detail["memory"] = "degraded"
        logger.warning(
            f"System memory {memory_percent:.1f}% is at or above the "
            f"{settings.MEMORY_USAGE_WARN_PERCENT}% threshold"
        )
    else:
        detail["memory"] = "ok"

    return detail


def collect_checks() -> dict:
    """Run every dependency check and return a status map.

    Each probe is timed and mirrored into Prometheus so dependency
    availability is queryable historically, not just at the moment a probe is
    called.
    """
    checks = {}
    for name, probe in (
        ("database", check_database),
        ("qdrant", check_qdrant),
        ("redis", check_redis),
        ("gemini", check_gemini),
        ("storage", check_storage),
    ):
        started = time.perf_counter()
        status = probe()
        metrics.safe(metrics.observe_dependency, name, status, time.perf_counter() - started)
        checks[name] = status

    resources = check_resources()
    checks["disk"] = resources["disk"]
    checks["memory"] = resources["memory"]
    return checks


# Statuses that count as healthy for reporting. "missing"/"disabled"/
# "unavailable" mean an optional dependency is simply not configured, which is
# not a failure.
_HEALTHY_STATUSES = {"ok", "configured", "disabled", "unavailable"}

# Statuses that do not block readiness. Resource pressure is deliberately
# included: a disk at 85% or memory at 90% is worth warning about, but failing
# readiness would pull the instance out of the load balancer and concentrate
# the same traffic onto fewer instances - making the incident worse. /api/health
# still reports it as degraded so it is visible and alertable.
_READY_STATUSES = _HEALTHY_STATUSES | {"degraded"}


def uptime_seconds() -> float:
    if START_TIME is None:
        return 0.0
    return round((datetime.now(UTC) - START_TIME).total_seconds(), 2)


# ---------------------------------------------------------------------------
# Lifespan - startup / shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify every dependency on startup and release clients on shutdown."""
    global START_TIME, qdrant_client, redis_client

    # --- Startup -----------------------------------------------------------
    setup_logging()  # idempotent; guarantees JSON logging even under Gunicorn
    START_TIME = datetime.now(UTC)
    logger.info(
        f"Starting {settings.APP_NAME} v{settings.APP_VERSION} "
        f"(environment={settings.ENVIRONMENT})"
    )

    # Configuration validation. In production a misconfiguration aborts
    # startup; outside production the same findings are logged as warnings so
    # local development is not blocked.
    config_report = validate_configuration(settings)
    for warning in config_report["warnings"]:
        logger.warning(f"[config] {warning}")
    if config_report["errors"]:
        for error in config_report["errors"]:
            logger.error(f"[config] {error}")
        raise ConfigurationError(
            "Refusing to start: "
            f"{len(config_report['errors'])} invalid production configuration "
            "setting(s): " + "; ".join(config_report["errors"])
        )
    if not config_report["warnings"]:
        logger.info("[config] configuration validated")

    # Long-lived clients, created once and reused by the health checks
    if QdrantClient is not None:
        try:
            qdrant_client = QdrantClient(
                host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=5
            )
        except Exception as exc:
            logger.error(f"Could not construct Qdrant client: {exc}")
            qdrant_client = None
    if redis is not None and settings.redis_enabled:
        try:
            redis_client = redis.from_url(settings.REDIS_URL, socket_connect_timeout=5)
        except Exception as exc:
            logger.error(f"Could not construct Redis client: {exc}")
            redis_client = None

    checks = collect_checks()
    for name, status in checks.items():
        if status in ("ok", "configured"):
            logger.info(f"[startup] {name}: {status}")
        elif status in ("disabled", "unavailable"):
            logger.warning(f"[startup] {name}: {status} (optional, skipped)")
        else:
            logger.error(f"[startup] {name}: {status}")

    # The schema is owned by Alembic, not by the application. create_all used
    # to run here outside production; it creates missing tables but never
    # alters an existing one, so a drifted database looked healthy until a
    # query hit a column that was never added. Startup now verifies the
    # recorded revision and refuses to run against a schema this build was
    # not written for.
    if checks["database"] == "ok":
        await run_in_threadpool(verify_schema, engine)

    # Realtime. The manager owns the heartbeat reaper and the broker owns the
    # Redis subscriber; both are background tasks, and a background task with
    # no owner is a leak. Neither needs Redis in order to start.
    if settings.WS_ENABLED:
        # The reaper also re-checks each socket's authorization; see
        # app/realtime/revalidation.py.
        await get_manager().start(validator=revalidate)
        await get_broker().start()
        logger.info("[startup] realtime: listening")

    degraded = [n for n, s in checks.items() if s not in _HEALTHY_STATUSES]
    if degraded:
        logger.warning(f"Startup complete with degraded dependencies: {', '.join(degraded)}")
    else:
        logger.info("Startup complete - all dependencies healthy")

    yield

    # --- Shutdown ----------------------------------------------------------
    logger.info("Shutdown initiated - releasing resources")
    # First: connected clients are told to go away rather than left to
    # discover a broken pipe, which is what makes their reconnect
    # immediate instead of a heartbeat timeout away.
    #
    # Manager before broker. Closing a socket starts its teardown, and the
    # teardown publishes the departure through the broker; stopping the
    # broker first made that publish open a fresh Redis connection that
    # nothing would ever close. stop() waits for the handlers to finish.
    try:
        await get_manager().stop()
        await get_broker().stop()
        logger.info("[shutdown] realtime connections closed")
    except Exception as exc:
        logger.error(f"[shutdown] error closing realtime connections: {exc}")
    if qdrant_client is not None:
        try:
            qdrant_client.close()
            logger.info("[shutdown] Qdrant client closed")
        except Exception as exc:
            logger.error(f"[shutdown] error closing Qdrant client: {exc}")
        finally:
            qdrant_client = None
    if redis_client is not None:
        try:
            redis_client.close()
            logger.info("[shutdown] Redis client closed")
        except Exception as exc:
            logger.error(f"[shutdown] error closing Redis client: {exc}")
        finally:
            redis_client = None
    try:
        engine.dispose()
        logger.info("[shutdown] database connection pool disposed")
    except Exception as exc:
        logger.error(f"[shutdown] error disposing database pool: {exc}")
    logger.info(f"Shutdown complete (uptime {uptime_seconds()}s)")


# ---------------------------------------------------------------------------
# FastAPI application with lifespan events
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    """Factory that creates the FastAPI application.

    The function is kept separate to aid testing and to ensure a clean
    module-level namespace.
    """

    app = FastAPI(
        title="ResearchSphere AI - Enterprise Backend Gateway",
        description="Production RAG pipeline, LangGraph Multi-Agent Workflows, and MCP Connectors Engine",
        version=settings.APP_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # -------------------------------------------------------------------
    # Middleware registration (order matters)
    # -------------------------------------------------------------------
    # Registration order below is the documented pipeline order. Starlette wraps
    # middleware in reverse, so the last one registered (CORS) is the outermost
    # at runtime - which is what we want: CORS headers are then applied to every
    # response, including 400s from TrustedHost and 429s from the rate limiter.
    #
    #   TrustedHost -> RequestID -> Logging -> SecurityHeaders -> RateLimit -> GZip -> CORS
    #
    # 1. Trusted hosts - reject unknown Host headers
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)
    # 2. Request ID - correlation ID for every request
    app.add_middleware(RequestIDMiddleware)
    # 3. Structured logging
    app.add_middleware(LoggingMiddleware)
    # 3b. Metrics. Registered inside RequestIDMiddleware so slow-request
    # warnings carry the correlation id, and outside routing so the matched
    # route template is available for low-cardinality labels.
    if settings.METRICS_ENABLED:
        app.add_middleware(MetricsMiddleware)
    # 4. Security headers
    app.add_middleware(SecurityHeadersMiddleware)
    # 5. Rate limiting
    app.add_middleware(RateLimitingMiddleware)
    # 6. Compression
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # 7. CORS - explicit origin allowlist loaded from configuration.
    # Wildcards are rejected in production; they are also incompatible with
    # allow_credentials=True, which browsers refuse alongside "*".
    allow_origins = settings.cors_origins
    if "*" in allow_origins:
        if settings.is_production:
            raise RuntimeError(
                "Wildcard CORS origin '*' is not permitted in production. "
                "Set CORS_ORIGINS to an explicit comma-separated allowlist."
            )
        logger.warning("Wildcard CORS origin enabled - development only")
    logger.info(f"CORS allowed origins: {allow_origins}")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-RateLimit-Remaining"],
    )

    # -------------------------------------------------------------------
    # Exception handlers
    # -------------------------------------------------------------------
    app.add_exception_handler(AppException, app_exception_handler)
    # Registered against Starlette's HTTPException, which FastAPI's subclasses.
    # Registering the subclass alone left Starlette's own errors -- an unmatched
    # route, a TrustedHost rejection -- returning a bare {"detail": ...} instead
    # of the standard envelope, so those responses carried no request_id to
    # correlate with the logs.
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)

    # -------------------------------------------------------------------
    # Router registration (versioned API)
    # -------------------------------------------------------------------
    app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authentication"])
    app.include_router(workspaces_router, prefix="/api/v1/workspaces", tags=["Workspaces"])
    # Mounted under the same prefix: members and invitations belong to a
    # workspace, and keeping them there means one place to reason about
    # workspace-scoped authorization.
    app.include_router(members_router, prefix="/api/v1/workspace", tags=["Workspace members"])
    app.include_router(documents_router, prefix="/api/v1/documents", tags=["Documents"])
    app.include_router(chat_router, prefix="/api/v1/chat", tags=["RAG Chat"])
    app.include_router(research_router, prefix="/api/v1/research", tags=["LangGraph Agents"])
    app.include_router(reports_router, prefix="/api/v1/reports", tags=["Report Generator"])
    app.include_router(mcp_router, prefix="/api/v1/mcp", tags=["MCP Connectors"])
    app.include_router(analytics_router, prefix="/api/v1/analytics", tags=["Analytics Telemetry"])
    app.include_router(admin_router, prefix="/api/v1/admin", tags=["Admin & System Health"])
    # One endpoint rather than a resource, so no prefix of its own. Note
    # that every HTTP middleware above skips it -- see the module docstring
    # in app/api/v1/websocket.py for what that means.
    app.include_router(websocket_router, prefix="/api/v1", tags=["Realtime"])

    # -------------------------------------------------------------------
    # Health endpoints - liveness, readiness and full diagnostics
    # -------------------------------------------------------------------
    @app.get("/api/health", tags=["Health"])
    async def health():
        """Full diagnostics: per-dependency status, uptime and version.

        Always returns 200 so the payload stays readable by dashboards even
        while the service is degraded; use ``/api/ready`` for gating.
        """
        checks = await run_in_threadpool(collect_checks)
        degraded = [n for n, s in checks.items() if s not in _HEALTHY_STATUSES]
        return {
            "status": "degraded" if degraded else "healthy",
            "service": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "uptime_seconds": uptime_seconds(),
            "checks": checks,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @app.get("/api/live", tags=["Health"])
    async def live():
        """Liveness probe - the process is running. No dependency I/O."""
        return {"status": "live", "uptime_seconds": uptime_seconds()}

    @app.get("/api/ready", tags=["Health"])
    async def ready():
        """Readiness probe - 503 when a required dependency is unavailable."""
        checks = await run_in_threadpool(collect_checks)
        failed = [n for n, s in checks.items() if s not in _READY_STATUSES]
        payload = {
            "status": "unavailable" if failed else "ready",
            "version": settings.APP_VERSION,
            "uptime_seconds": uptime_seconds(),
            "checks": checks,
        }
        if failed:
            payload["failed"] = failed
            logger.warning(f"Readiness probe failed: {', '.join(failed)}")
        return JSONResponse(status_code=503 if failed else 200, content=payload)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics(request: Request):
        """Prometheus exposition endpoint.

        Optionally bearer-protected via METRICS_TOKEN. In most deployments the
        route is instead restricted at the reverse proxy, which is why the
        token is off by default rather than a mandatory secret.
        """
        if not settings.METRICS_ENABLED:
            return JSONResponse(status_code=404, content={"detail": "Metrics are disabled"})

        if settings.METRICS_TOKEN:
            supplied = request.headers.get("Authorization", "")
            expected = f"Bearer {settings.METRICS_TOKEN}"
            # Constant-time comparison: this guards a scrape credential.
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

        # Refresh the gauges that are sampled rather than incremented.
        metrics.safe(metrics.collect_process_metrics)
        metrics.safe(metrics.set_app_info)
        payload = await run_in_threadpool(metrics.render)
        return Response(content=payload, media_type=metrics.CONTENT_TYPE)

    # Legacy unprefixed aliases - kept so existing probes keep working.
    app.add_api_route("/health", health, methods=["GET"], include_in_schema=False)
    app.add_api_route("/live", live, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ready", ready, methods=["GET"], include_in_schema=False)

    return app


# ---------------------------------------------------------------------------
# Create the FastAPI instance
# ---------------------------------------------------------------------------
app = create_app()

# ---------------------------------------------------------------------------
# Development entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    # nosec B104 - binding all interfaces is required inside a container; the
    # edge proxy is what is actually exposed. This branch is development only.
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)  # nosec B104
