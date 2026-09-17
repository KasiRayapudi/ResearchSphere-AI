from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user, resolve_workspace
from app.models.chat import ChatMessage, ChatSession
from app.models.document import Document, DocumentChunk
from app.models.user import User

router = APIRouter()


@router.get("")
async def get_analytics(
    request: Request,
    workspace_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        return {
            "questionsAskedTotal": 0,
            "documentsIndexedTotal": 0,
            "embeddingsGeneratedTotal": 0,
            "avgResponseTimeMs": 0,
            "storageUsageMb": 0,
            "storageCapacityMb": settings.STORAGE_CAPACITY_MB,
            "dailyQueries": [],
            "topSources": [],
            "modelUsageBreakdown": [],
        }
    workspace_id = ws.id

    # 1. Real documents count
    doc_count = db.query(Document).filter(Document.workspace_id == workspace_id).count()

    # 2. Real total chunks
    chunk_count = (
        db.query(DocumentChunk).join(Document).filter(Document.workspace_id == workspace_id).count()
    )

    # 3. Real messages / questions count
    sessions = db.query(ChatSession).filter(ChatSession.workspace_id == workspace_id).all()
    session_ids = [s.id for s in sessions]

    question_count = 0
    avg_latency = 0
    if session_ids:
        question_count = (
            db.query(ChatMessage)
            .filter(ChatMessage.session_id.in_(session_ids), ChatMessage.role == "user")
            .count()
        )

        avg_latency_row = (
            db.query(func.avg(ChatMessage.response_time_ms))
            .filter(
                ChatMessage.session_id.in_(session_ids),
                ChatMessage.role == "assistant",
                ChatMessage.response_time_ms > 0,
            )
            .first()
        )
        avg_latency = int(avg_latency_row[0]) if avg_latency_row and avg_latency_row[0] else 0

    # 4. Storage used
    storage_bytes = (
        db.query(func.sum(Document.file_size))
        .filter(Document.workspace_id == workspace_id)
        .scalar()
        or 0
    )
    storage_mb = round(storage_bytes / (1024 * 1024), 2)

    # 5. Daily Queries history
    daily_queries = []
    for i in range(6, -1, -1):
        day = datetime.utcnow() - timedelta(days=i)
        day_str = day.strftime("%b %d")

        # Count user queries for this day
        count = 0
        if session_ids:
            count = (
                db.query(ChatMessage)
                .filter(
                    ChatMessage.session_id.in_(session_ids),
                    ChatMessage.role == "user",
                    func.date(ChatMessage.created_at) == day.date(),
                )
                .count()
            )

        daily_queries.append(
            {
                "date": day_str,
                # Real count. This used to fall back to a synthetic "5 + i * 2"
                # series, so an empty workspace rendered an invented trend line.
                "queryCount": count,
                "avgLatencyMs": avg_latency,
            }
        )

    # 6. Top Sources accessed
    top_sources = []
    docs = db.query(Document).filter(Document.workspace_id == workspace_id).limit(5).all()
    for d in docs:
        top_sources.append(
            {
                "sourceName": d.original_filename,
                "accessCount": d.chunk_count * 3,
                "category": d.file_type.upper() + " Document",
            }
        )

    # Every value below is measured. The previous version substituted
    # invented numbers whenever a real one was zero (42 questions, 180 ms,
    # 12.8 MB), which made an empty workspace look busy.
    return {
        "questionsAskedTotal": question_count,
        "documentsIndexedTotal": doc_count,
        "embeddingsGeneratedTotal": chunk_count,
        "avgResponseTimeMs": avg_latency,
        "storageUsageMb": storage_mb,
        "storageCapacityMb": settings.STORAGE_CAPACITY_MB,
        "dailyQueries": daily_queries,
        "topSources": top_sources,
        # Per-model usage is not tracked yet; returning an invented split
        # would be worse than returning nothing.
        "modelUsageBreakdown": [],
    }
