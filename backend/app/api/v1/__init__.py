from app.api.v1.auth import router as auth_router
from app.api.v1.workspaces import router as workspaces_router
from app.api.v1.documents import router as documents_router
from app.api.v1.chat import router as chat_router
from app.api.v1.research import router as research_router
from app.api.v1.reports import router as reports_router
from app.api.v1.mcp import router as mcp_router
from app.api.v1.analytics import router as analytics_router
from app.api.v1.admin import router as admin_router

__all__ = [
    "auth_router",
    "workspaces_router",
    "documents_router",
    "chat_router",
    "research_router",
    "reports_router",
    "mcp_router",
    "analytics_router",
    "admin_router",
]
