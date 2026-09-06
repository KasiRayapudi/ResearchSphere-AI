from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import get_current_user, resolve_workspace
from app.models.user import User
from app.models.workspace import Workspace
from app.models.chat import ChatSession, ChatMessage
from app.rag.pipeline import stream_rag_response
from app.core.logging import get_logger
from app.core.audit import audit, AuditAction, AuditOutcome
import json
import asyncio
from typing import Optional, List

router = APIRouter()
logger = get_logger("chat")

class ChatQuery(BaseModel):
    prompt: str
    workspace_id: Optional[str] = None
    session_id: Optional[str] = None
    model: str = "Gemini 1.5 Pro"

@router.get("/sessions")
async def get_sessions(
    request: Request,
    workspace_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        return []
    workspace_id = ws.id

    sessions = db.query(ChatSession).filter(
        ChatSession.workspace_id == workspace_id,
        ChatSession.user_id == current_user.id
    ).order_by(ChatSession.updated_at.desc()).all()
    
    return [
        {
            "id": s.id,
            "title": s.title or "New Chat Session",
            "workspaceId": s.workspace_id,
            "createdAt": s.created_at.isoformat(),
        }
        for s in sessions
    ]

@router.post("/sessions")
async def create_session(
    request: Request,
    workspace_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        raise HTTPException(status_code=400, detail="No active workspace found")
    workspace_id = ws.id
        
    session = ChatSession(
        workspace_id=workspace_id,
        user_id=current_user.id,
        title="New Chat Session"
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
    current_user: User = Depends(get_current_user)
):
    # 1. Determine active workspace
    workspace_id = payload.workspace_id
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        raise HTTPException(status_code=400, detail="Workspace required")
    workspace_id = ws.id

    # 2. Get or create session
    session_id = payload.session_id
    if not session_id:
        session = db.query(ChatSession).filter(
            ChatSession.workspace_id == workspace_id,
            ChatSession.user_id == current_user.id
        ).first()
        if not session:
            session = ChatSession(
                workspace_id=workspace_id,
                user_id=current_user.id,
                title=payload.prompt[:40]
            )
            db.add(session)
            db.commit()
            db.refresh(session)
        session_id = session.id
    else:
        # Never trust a client-supplied session id: it must belong to this
        # user and to the resolved workspace.
        session = db.query(ChatSession).filter(
            ChatSession.id == session_id,
            ChatSession.user_id == current_user.id,
            ChatSession.workspace_id == workspace_id,
        ).first()
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
        session_id=session_id,
        role="user",
        content=payload.prompt,
        model_used=payload.model
    )
    db.add(user_msg)
    db.commit()

    async def event_generator():
        accumulated_text = ""
        sources_meta = []
        response_time = 0

        try:
            # Stream real RAG pipeline output
            async for chunk in stream_rag_response(
                question=payload.prompt,
                workspace_id=workspace_id
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
                    yield f"data: {json.dumps({'text': chunk})}\n\n"
                    accumulated_text += chunk

                await asyncio.sleep(0.01)
        except Exception as exc:
            # A retrieval or model failure must not escape the generator: once
            # the response has started streaming the status code is already
            # sent, so an exception here would truncate the stream with no
            # explanation. Emit a typed error event instead.
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
                response_time_ms=response_time
            )
            db.add(assistant_msg)
            db.commit()
        except Exception as exc:
            logger.error(f"Failed to persist assistant message: {exc}", exc_info=True)
            db.rollback()

        # Send completed session marker
        yield f"data: [DONE]\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
