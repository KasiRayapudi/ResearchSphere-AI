from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def get_system_health():
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
async def get_feature_flags():
    return [
        {"id": "ff-1", "key": "mcp_gdrive_sync", "name": "Google Drive Live MCP Sync", "description": "Enable background folder watching and instant chunk updates.", "enabled": True, "targetRole": "All Roles"},
        {"id": "ff-2", "key": "deep_critic_verifier", "name": "Deep Critic Fact Verifier", "description": "Run secondary cross-encoder validation before streaming model tokens.", "enabled": True, "targetRole": "Pro & Enterprise"},
        {"id": "ff-3", "key": "voice_input_stream", "name": "Voice Input Streaming", "description": "Enable WebSpeech API audio input for natural voice queries.", "enabled": True, "targetRole": "Beta Testers"},
        {"id": "ff-4", "key": "auto_pdf_ocr_ingestion", "name": "Automated Tesseract OCR Ingestion", "description": "Extract text from scanned PDF tables and image figures during chunking.", "enabled": True, "targetRole": "All Roles"},
    ]
