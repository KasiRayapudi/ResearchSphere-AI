import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from fastapi import FastAPI, HTTPException
from starlette.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from app.core.logging import setup_logging, get_logger
from app.core.middleware import (
    RequestIDMiddleware,
    LoggingMiddleware,
    SecurityHeadersMiddleware,
    RateLimitingMiddleware,
)
from app.core.exception_handlers import (
    app_exception_handler,
    http_exception_handler,
    validation_exception_handler,
    generic_exception_handler,
)
from app.core.exceptions import AppException
from app.core.config import settings, validate_configuration, ConfigurationError
from app.api.v1 import (
    auth_router,
    workspaces_router,
    documents_router,
    chat_router,
    research_router,
    reports_router,
    mcp_router,
    analytics_router,
    admin_router,
)
from app.core.database import Base, engine
import app.models  # Ensure all models are imported for metadata creation

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
        client = redis_client or redis.from_url(
            settings.REDIS_URL, socket_connect_timeout=5
        )
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


def collect_checks() -> dict:
    """Run every dependency check and return a status map."""
    return {
        "database": check_database(),
        "qdrant": check_qdrant(),
        "redis": check_redis(),
        "gemini": check_gemini(),
        "storage": check_storage(),
    }


# Statuses that do not block readiness. "missing"/"disabled"/"unavailable" mean
# an optional dependency is simply not configured, which is not a failure.
_HEALTHY_STATUSES = {"ok", "configured", "disabled", "unavailable"}


def uptime_seconds() -> float:
    if START_TIME is None:
        return 0.0
    return round((datetime.now(timezone.utc) - START_TIME).total_seconds(), 2)


# ---------------------------------------------------------------------------
# Lifespan - startup / shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify every dependency on startup and release clients on shutdown."""
    global START_TIME, qdrant_client, redis_client

    # --- Startup -----------------------------------------------------------
    setup_logging()  # idempotent; guarantees JSON logging even under Gunicorn
    START_TIME = datetime.now(timezone.utc)
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
            redis_client = redis.from_url(
                settings.REDIS_URL, socket_connect_timeout=5
            )
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

    # Development convenience: ensure tables exist. Production uses migrations.
    if not settings.is_production and checks["database"] == "ok":
        try:
            Base.metadata.create_all(bind=engine)
            logger.info("[startup] database schema ensured (development mode)")
        except Exception as exc:
            logger.error(f"[startup] schema creation failed: {exc}")

    degraded = [n for n, s in checks.items() if s not in _HEALTHY_STATUSES]
    if degraded:
        logger.warning(
            f"Startup complete with degraded dependencies: {', '.join(degraded)}"
        )
    else:
        logger.info("Startup complete - all dependencies healthy")

    yield

    # --- Shutdown ----------------------------------------------------------
    logger.info("Shutdown initiated - releasing resources")
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
    module‑level namespace.
    """

    app = FastAPI(
        title="ResearchSphere AI - Enterprise Backend Gateway",
        description="Production RAG pipeline, LangGraph Multi‑Agent Workflows, and MCP Connectors Engine",
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
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)

    # -------------------------------------------------------------------
    # Router registration (versioned API)
    # -------------------------------------------------------------------
    app.include_router(auth_router, prefix="/api/v1/auth", tags=["Authentication"])
    app.include_router(workspaces_router, prefix="/api/v1/workspaces", tags=["Workspaces"])
    app.include_router(documents_router, prefix="/api/v1/documents", tags=["Documents"])
    app.include_router(chat_router, prefix="/api/v1/chat", tags=["RAG Chat"])
    app.include_router(research_router, prefix="/api/v1/research", tags=["LangGraph Agents"])
    app.include_router(reports_router, prefix="/api/v1/reports", tags=["Report Generator"])
    app.include_router(mcp_router, prefix="/api/v1/mcp", tags=["MCP Connectors"])
    app.include_router(analytics_router, prefix="/api/v1/analytics", tags=["Analytics Telemetry"])
    app.include_router(admin_router, prefix="/api/v1/admin", tags=["Admin & System Health"])

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
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/live", tags=["Health"])
    async def live():
        """Liveness probe - the process is running. No dependency I/O."""
        return {"status": "live", "uptime_seconds": uptime_seconds()}

    @app.get("/api/ready", tags=["Health"])
    async def ready():
        """Readiness probe - 503 when a required dependency is unavailable."""
        checks = await run_in_threadpool(collect_checks)
        failed = [n for n, s in checks.items() if s not in _HEALTHY_STATUSES]
        payload = {
            "status": "unavailable" if failed else "ready",
            "version": settings.APP_VERSION,
            "uptime_seconds": uptime_seconds(),
            "checks": checks,
        }
        if failed:
            payload["failed"] = failed
            logger.warning(f"Readiness probe failed: {', '.join(failed)}")
        return JSONResponse(
            status_code=503 if failed else 200, content=payload
        )

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
# Development entry‑point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
