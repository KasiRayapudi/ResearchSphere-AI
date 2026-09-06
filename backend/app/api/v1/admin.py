from fastapi import APIRouter, Request
from app.core.audit import audit, AuditAction, AuditOutcome

router = APIRouter()

@router.get("/health")
async def get_system_health(request: Request):
    # NOTE: this endpoint is not yet authenticated - see Sprint 2 Milestone 8.
    audit(
        action=AuditAction.ADMIN_ACCESS,
        outcome=AuditOutcome.SUCCESS,
        resource="admin:system_health",
        request=request,
    )
    return {
        "postgressStatus": "healthy",
        "qdrantStatus": "healthy",
        "redisStatus": "healthy",
        "fastapiStatus": "healthy",
        "celeryStatus": "healthy",
        "uptimeSeconds": 1428900,
        "activeAgentsCount": 8,
    }

@router.get("/feature-flags")
async def get_feature_flags(request: Request):
    audit(
        action=AuditAction.ADMIN_ACCESS,
        outcome=AuditOutcome.SUCCESS,
        resource="admin:feature_flags",
        request=request,
    )
    return [
        {"id": "ff-1", "key": "mcp_gdrive_sync", "name": "Google Drive Live MCP Sync", "description": "Enable background folder watching and instant chunk updates.", "enabled": True, "targetRole": "All Roles"},
        {"id": "ff-2", "key": "deep_critic_verifier", "name": "Deep Critic Fact Verifier", "description": "Run secondary cross-encoder validation before streaming model tokens.", "enabled": True, "targetRole": "Pro & Enterprise"},
        {"id": "ff-3", "key": "voice_input_stream", "name": "Voice Input Streaming", "description": "Enable WebSpeech API audio input for natural voice queries.", "enabled": True, "targetRole": "Beta Testers"},
        {"id": "ff-4", "key": "auto_pdf_ocr_ingestion", "name": "Automated Tesseract OCR Ingestion", "description": "Extract text from scanned PDF tables and image figures during chunking.", "enabled": True, "targetRole": "All Roles"},
    ]
