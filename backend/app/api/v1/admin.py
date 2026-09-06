"""
Administrative endpoints.

Every route requires an authenticated user holding the ``admin`` role and
reports real system state - there are no hardcoded values. Where a subsystem
genuinely does not exist yet (Celery), it is reported as ``not_configured``
rather than being invented.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.audit import audit, AuditAction, AuditOutcome
from app.core.config import settings
from app.core.database import get_db
from app.core.security import require_admin
from app.models.chat import ChatMessage, ChatSession
from app.models.document import Document, DocumentChunk
from app.models.report import Connector, Report
from app.models.user import User
from app.models.workspace import Workspace

router = APIRouter()


def _status_word(check: str) -> str:
    """Map a dependency check result onto the status vocabulary the UI uses."""
    return {
        "ok": "healthy",
        "configured": "healthy",
        "created": "healthy",
        "disabled": "not_configured",
        "unavailable": "not_configured",
        "missing": "not_configured",
        "failed": "unhealthy",
    }.get(check, "unknown")


@router.get("/health")
async def get_system_health(
    request: Request,
    current_user: User = Depends(require_admin),
):
    """Real system health, derived from the same checks the probes use."""
    # Imported lazily to avoid a circular import at module load.
    from main import collect_checks, uptime_seconds

    checks = collect_checks()

    audit(
        action=AuditAction.ADMIN_ACCESS,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource="admin:system_health",
        request=request,
    )

    return {
        "postgresStatus": _status_word(checks["database"]),
        # Retained for backwards compatibility with the existing frontend,
        # which reads the original misspelled key.
        "postgressStatus": _status_word(checks["database"]),
        "qdrantStatus": _status_word(checks["qdrant"]),
        "redisStatus": _status_word(checks["redis"]),
        "geminiStatus": _status_word(checks["gemini"]),
        "storageStatus": _status_word(checks["storage"]),
        "fastapiStatus": "healthy",
        # No Celery worker exists in this deployment. Reporting it as healthy
        # would be a fabrication.
        "celeryStatus": "not_configured",
        "uptimeSeconds": int(uptime_seconds()),
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "checks": checks,
    }


@router.get("/stats")
async def get_system_stats(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Real platform statistics, computed from the database."""
    since_24h = datetime.utcnow() - timedelta(hours=24)
    since_7d = datetime.utcnow() - timedelta(days=7)

    storage_bytes = db.query(func.sum(Document.file_size)).scalar() or 0

    stats = {
        "users": {
            "total": db.query(User).count(),
            "active": db.query(User).filter(User.is_active.is_(True)).count(),
            "admins": db.query(User).filter(User.role == "admin").count(),
            "newLast7Days": db.query(User).filter(User.created_at >= since_7d).count(),
        },
        "workspaces": {
            "total": db.query(Workspace).count(),
        },
        "documents": {
            "total": db.query(Document).count(),
            "indexed": db.query(Document).filter(Document.status == "indexed").count(),
            "failed": db.query(Document).filter(Document.status == "failed").count(),
            "processing": db.query(Document).filter(Document.status == "processing").count(),
            "chunks": db.query(DocumentChunk).count(),
            "storageBytes": int(storage_bytes),
            "storageMb": round(storage_bytes / (1024 * 1024), 2),
        },
        "chat": {
            "sessions": db.query(ChatSession).count(),
            "messages": db.query(ChatMessage).count(),
            "messagesLast24h": db.query(ChatMessage)
            .filter(ChatMessage.created_at >= since_24h)
            .count(),
        },
        "reports": {
            "total": db.query(Report).count(),
        },
        "connectors": {
            "total": db.query(Connector).count(),
            "active": db.query(Connector).filter(Connector.status == "active").count(),
        },
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }

    audit(
        action=AuditAction.ADMIN_ACCESS,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource="admin:stats",
        request=request,
    )
    return stats


@router.get("/users")
async def list_users(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """List real user accounts. Password hashes are never returned."""
    limit = max(1, min(limit, 200))
    total = db.query(User).count()
    users = (
        db.query(User)
        .order_by(User.created_at.desc())
        .offset(max(0, offset))
        .limit(limit)
        .all()
    )

    audit(
        action=AuditAction.ADMIN_ACCESS,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource="admin:users",
        request=request,
        metadata={"limit": limit, "offset": offset},
    )

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": u.id,
                "email": u.email,
                "name": u.full_name,
                "role": u.role,
                "isActive": u.is_active,
                "createdAt": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }


@router.get("/feature-flags")
async def get_feature_flags(
    request: Request,
    current_user: User = Depends(require_admin),
):
    """Feature flags derived from actual runtime configuration.

    These reflect real settings rather than a hardcoded catalogue: each entry
    reports whether the capability is genuinely enabled in this deployment.
    """
    audit(
        action=AuditAction.ADMIN_ACCESS,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource="admin:feature_flags",
        request=request,
    )

    return [
        {
            "id": "ff-duplicate-detection",
            "key": "upload_duplicate_detection",
            "name": "Upload Duplicate Detection",
            "description": "Reject re-uploads of identical content using SHA-256.",
            "enabled": settings.ENABLE_DUPLICATE_DETECTION,
            "source": "ENABLE_DUPLICATE_DETECTION",
        },
        {
            "id": "ff-virus-scanning",
            "key": "upload_virus_scanning",
            "name": "Upload Virus Scanning",
            "description": "Scan quarantined uploads before promoting them to storage.",
            "enabled": settings.VIRUS_SCANNER.lower() not in ("noop", "none", "disabled", ""),
            "source": "VIRUS_SCANNER",
        },
        {
            "id": "ff-security-headers",
            "key": "security_headers",
            "name": "Security Headers",
            "description": "Emit CSP, COOP, CORP and related response headers.",
            "enabled": settings.SECURITY_HEADERS_ENABLED,
            "source": "SECURITY_HEADERS_ENABLED",
        },
        {
            "id": "ff-hsts",
            "key": "hsts",
            "name": "HTTP Strict Transport Security",
            "description": "Sent only in production over HTTPS.",
            "enabled": settings.HSTS_ENABLED and settings.is_production,
            "source": "HSTS_ENABLED",
        },
        {
            "id": "ff-redis",
            "key": "redis_backed_features",
            "name": "Redis-Backed Rate Limiting",
            "description": "Distributed rate limiting and token revocation.",
            "enabled": settings.redis_enabled,
            "source": "REDIS_URL",
        },
    ]
