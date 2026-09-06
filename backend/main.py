import os
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
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
from app.core.config import settings
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
        version="2.4.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # -------------------------------------------------------------------
    # Middleware registration (order matters)
    # -------------------------------------------------------------------
    # Trusted hosts – restrict to known hostnames (fallback to localhost)
    trusted_hosts = getattr(settings, "TRUSTED_HOSTS", ["localhost", "127.0.0.1"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    app.add_middleware(RequestIDMiddleware)            # 1️⃣ Request ID
    app.add_middleware(LoggingMiddleware)              # 2️⃣ Structured logging
    app.add_middleware(SecurityHeadersMiddleware)       # 3️⃣ Security headers
    app.add_middleware(RateLimitingMiddleware)         # 4️⃣ Rate limiting
    app.add_middleware(GZipMiddleware, minimum_size=1024)  # 5️⃣ Compression

    # Trusted hosts – restrict to known hostnames (fallback to localhost)
    trusted_hosts = getattr(settings, "TRUSTED_HOSTS", ["localhost", "127.0.0.1"])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)

    # CORS – restrict to the configured frontend URL (single origin string)
    frontend_origin = getattr(settings, "FRONTEND_URL", "http://localhost:3000")
    allow_origins = [frontend_origin]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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
    # Health endpoints – verify core services
    # -------------------------------------------------------------------
    @app.get("/health")
    async def health_check():
        """Basic liveness endpoint – always returns ``healthy``.

        Additional diagnostics are provided by ``/ready``.
        """
        return {
            "status": "healthy",
            "service": "ResearchSphere AI Gateway",
            "version": settings.APP_VERSION if hasattr(settings, "APP_VERSION") else "2.4.0",
        }

    @app.get("/live")
    async def liveness():
        return {"status": "live"}

    @app.get("/ready")
    async def readiness():
        """Readiness probe – checks DB, Qdrant, Redis, Gemini key and storage.
        Returns ``ready`` only when *all* checks succeed.
        """
        checks = {}
        # Database connectivity
        try:
            with engine.connect() as conn:
                conn.execute("SELECT 1")
            checks["database"] = "ok"
        except Exception as exc:
            logger.error(f"Readiness DB check failed: {exc}")
            checks["database"] = "failed"
        # Qdrant connectivity
        if QdrantClient:
            try:
                client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
                client.get_collection(collection_name=settings.QDRANT_COLLECTION)
                checks["qdrant"] = "ok"
            except Exception as exc:  # pragma: no cover
                logger.error(f"Readiness Qdrant check failed: {exc}")
                checks["qdrant"] = "failed"
        else:
            checks["qdrant"] = "unavailable"
        # Redis connectivity
        if redis:
            try:
                r = redis.from_url(settings.REDIS_URL)
                r.ping()
                checks["redis"] = "ok"
            except Exception as exc:  # pragma: no cover
                logger.error(f"Readiness Redis check failed: {exc}")
                checks["redis"] = "failed"
        else:
            checks["redis"] = "unavailable"
        # Gemini API configuration
        if getattr(settings, "GEMINI_API_KEY", None):
            checks["gemini"] = "configured"
        else:
            checks["gemini"] = "missing"
        # Storage directory existence
        upload_path = Path(getattr(settings, "UPLOAD_DIR", "./uploads"))
        if upload_path.exists():
            checks["storage"] = "ok"
        else:
            try:
                upload_path.mkdir(parents=True, exist_ok=True)
                checks["storage"] = "created"
            except Exception as exc:  # pragma: no cover
                logger.error(f"Readiness storage check failed: {exc}")
                checks["storage"] = "failed"
        overall = "ready" if all(v in ("ok", "configured", "created") for v in checks.values()) else "unavailable"
        status_code = 200 if overall == "ready" else 503
        return JSONResponse(status_code=status_code, content={"status": overall, "checks": checks})

    # -------------------------------------------------------------------
    # Lifespan events – startup / shutdown logging
    # -------------------------------------------------------------------
        @app.on_event("startup")
    async def on_startup():
        """Application startup hook.
        Performs connectivity checks for DB, Qdrant, Redis, and Gemini configuration.
        Records the start time for uptime calculations.
        """
        global start_time, qdrant_client, redis_client
        start_time = datetime.utcnow()
        # Database check
        try:
            with engine.connect() as conn:
                conn.execute("SELECT 1")
            logger.info("✅ Database connection established")
        except Exception as exc:
            logger.error(f"Database connection failed during startup: {exc}")
        # Qdrant check
        if QdrantClient:
            try:
                qdrant_client = QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
                qdrant_client.get_collection(collection_name=settings.QDRANT_COLLECTION)
                logger.info("✅ Qdrant connection established")
            except Exception as exc:
                logger.error(f"Qdrant connection failed during startup: {exc}")
                qdrant_client = None
        else:
            qdrant_client = None
        # Redis check
        if redis:
            try:
                redis_client = redis.from_url(settings.REDIS_URL)
                redis_client.ping()
                logger.info("✅ Redis connection established")
            except Exception as exc:
                logger.error(f"Redis connection failed during startup: {exc}")
                redis_client = None
        else:
            redis_client = None
        # Gemini configuration check
        if getattr(settings, "GEMINI_API_KEY", None):
            logger.info("✅ Gemini API key configured")
        else:
            logger.warning("⚠️ Gemini API key not configured")
        logger.info("🚀 Application startup complete")

    @app.on_event("shutdown")
    async def on_shutdown():
        """Application shutdown hook.
        Closes external resource connections if they were created.
        """
        logger.info("🛑 Application shutdown – cleaning up resources")
        # Close Qdrant client if applicable
        if 'qdrant_client' in globals() and qdrant_client:
            try:
                qdrant_client.close()
                logger.info("✅ Qdrant client closed")
            except Exception as exc:
                logger.error(f"Error closing Qdrant client: {exc}")
        # Close Redis client if applicable
        if 'redis_client' in globals() and redis_client:
            try:
                redis_client.close()
                logger.info("✅ Redis client closed")
            except Exception as exc:
                logger.error(f"Error closing Redis client: {exc}")
        logger.info("🚪 Shutdown complete")

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
