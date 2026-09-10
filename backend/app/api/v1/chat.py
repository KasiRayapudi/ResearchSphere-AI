import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.audit import AuditAction, AuditOutcome, audit
from app.core.database import get_db
from app.core.logging import get_logger
from app.core.pagination import Page, PageParams, page_params, paginate
from app.core.security import get_current_user, resolve_workspace
from app.core.tracking import capture_exception
from app.core.workspace_access import require_workspace_role
from app.models.chat import ChatMessage, ChatSession
from app.models.user import User
from app.rag.pipeline import stream_rag_response

router = APIRouter()
logger = get_logger("chat")


class ChatQuery(BaseModel):
    prompt: str
    workspace_id: str | None = None
    session_id: str | None = None
    model: str = "Gemini 1.5 Pro"


#: Sorting a chat sidebar by anything but recency is unusual, but title
#: ordering is cheap to offer and the allowlist has to be explicit anyway.
SESSION_SORTS = {
    "updatedAt": ChatSession.updated_at,
    "createdAt": ChatSession.created_at,
    "title": ChatSession.title,
}


@router.get("/sessions")
async def get_sessions(
    request: Request,
    workspace_id: str | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Chat sessions for the caller in this workspace.

    Scoped to the caller as well as the workspace: a conversation belongs to
    the person who had it, not to everyone who can read the workspace.
    """
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        return Page(
            items=[],
            page=params.page,
            page_size=params.page_size,
            total=0,
            pages=1,
            has_next=False,
            has_previous=False,
        ).envelope()

    query = db.query(ChatSession).filter(
        ChatSession.workspace_id == ws.id,
        ChatSession.user_id == current_user.id,
    )
    page = paginate(
        query,
        params,
        sortable=SESSION_SORTS,
        default_sort="updatedAt",
        tiebreaker=ChatSession.id,
        searchable=[ChatSession.title],
    )
    return page.envelope(
        [
            {
                "id": session.id,
                "title": session.title or "New Chat Session",
                "workspaceId": session.workspace_id,
                "createdAt": session.created_at.isoformat(),
            }
            for session in page.items
        ]
    )


@router.post("/sessions")
async def create_session(
    request: Request,
    workspace_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        raise HTTPException(status_code=400, detail="No active workspace found")
    # Creating a session writes to the workspace.
    require_workspace_role(request, ws, "content.write", current_user, db=db)
    workspace_id = ws.id

    session = ChatSession(
        workspace_id=workspace_id, user_id=current_user.id, title="New Chat Session"
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {"id": session.id, "title": session.title}


@router.post("/stream")
async def chat_stream(
    request: Request,
    payload: ChatQuery,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1. Determine active workspace
    workspace_id = payload.workspace_id
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        raise HTTPException(status_code=400, detail="Workspace required")
    # A chat turn stores messages against the workspace, so it is a write
    # even though it reads documents to answer.
    require_workspace_role(request, ws, "content.write", current_user, db=db)
    workspace_id = ws.id

    # 2. Get or create session
    session_id = payload.session_id
    if not session_id:
        session = (
            db.query(ChatSession)
            .filter(
                ChatSession.workspace_id == workspace_id, ChatSession.user_id == current_user.id
            )
            .first()
        )
        if not session:
            session = ChatSession(
                workspace_id=workspace_id, user_id=current_user.id, title=payload.prompt[:40]
            )
            db.add(session)
            db.commit()
            db.refresh(session)
        session_id = session.id
    else:
        # Never trust a client-supplied session id: it must belong to this
        # user and to the resolved workspace.
        session = (
            db.query(ChatSession)
            .filter(
                ChatSession.id == session_id,
                ChatSession.user_id == current_user.id,
                ChatSession.workspace_id == workspace_id,
            )
            .first()
        )
        if not session:
            audit(
                action=AuditAction.PERMISSION_DENIED,
                actor=current_user,
                outcome=AuditOutcome.DENIED,
                resource=f"chat_session:{session_id}",
                request=request,
                metadata={"reason": "not_owner_or_not_found"},
            )
            raise HTTPException(status_code=404, detail="Chat session not found")
        if session.title == "New Chat Session":
            session.title = payload.prompt[:40]
            db.commit()

    # 3. Add User message to DB
    user_msg = ChatMessage(
        session_id=session_id, role="user", content=payload.prompt, model_used=payload.model
    )
    db.add(user_msg)
    db.commit()

    async def event_generator():
        stream_started = time.perf_counter()
        first_token_at = None
        accumulated_text = ""
        sources_meta = []
        response_time = 0

        try:
            # Stream real RAG pipeline output
            async for chunk in stream_rag_response(
                question=payload.prompt, workspace_id=workspace_id
            ):
                if "__SOURCES_JSON__" in chunk:
                    # Parse metadata JSON payload
                    try:
                        parts = chunk.split("__SOURCES_JSON__")
                        # yield any text before token marker
                        if parts[0]:
                            yield f"data: {json.dumps({'text': parts[0]})}\n\n"
                            accumulated_text += parts[0]

                        meta_raw = parts[1].replace("__END_SOURCES__", "").strip()
                        meta_parsed = json.loads(meta_raw)
                        sources_meta = meta_parsed.get("__sources__", [])
                        response_time = meta_parsed.get("__response_time_ms__", 0)
                    except Exception as e:
                        logger.error(f"Error parsing RAG metadata: {e}")
                else:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                        # Time to first token is what a user perceives as
                        # responsiveness; total duration hides it entirely.
                        metrics.safe(
                            metrics.chat_time_to_first_token_seconds.observe,
                            first_token_at - stream_started,
                        )
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
                    accumulated_text += chunk

                await asyncio.sleep(0.01)
        except Exception as exc:
            # A retrieval or model failure must not escape the generator: once
            # the response has started streaming the status code is already
            # sent, so an exception here would truncate the stream with no
            # explanation. Emit a typed error event instead.
            metrics.safe(metrics.chat_messages_total.labels(outcome="failed").inc)
            capture_exception(exc, route="chat.stream", workspace_id=workspace_id)
            logger.error(f"Chat stream failed: {exc}", exc_info=True)
            yield f"data: {json.dumps({'error': 'The assistant is temporarily unavailable. Please try again.'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        # Write Assistant response message to DB
        try:
            assistant_msg = ChatMessage(
                session_id=session_id,
                role="assistant",
                content=accumulated_text,
                sources=sources_meta,
                model_used=payload.model,
                response_time_ms=response_time,
            )
            db.add(assistant_msg)
            db.commit()
        except Exception as exc:
            logger.error(f"Failed to persist assistant message: {exc}", exc_info=True)
            db.rollback()

        metrics.safe(metrics.chat_messages_total.labels(outcome="completed").inc)
        metrics.safe(
            metrics.chat_stream_duration_seconds.observe, time.perf_counter() - stream_started
        )
        # Send completed session marker
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
