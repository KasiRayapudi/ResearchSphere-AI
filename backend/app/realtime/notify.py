"""
What the rest of the application calls to say something happened.

Handlers should not build envelopes or know about Redis. They call one
function here, after their change is committed, and it is delivered to
whoever is connected.

**Notification is never allowed to fail the thing it describes.** Every
function here swallows its errors and logs them. A document that indexed
successfully has indexed successfully, whether or not a browser was told;
the client finds out by asking instead of by being told, which is the
behaviour that existed before any of this.

**Payloads mirror the REST responses.** A client applies an event directly to
state it originally loaded over HTTP, so the two describing the same thing
differently would mean every screen needed a translation layer.

**The audience matches what the REST API would return to the same person.**
Documents and members are visible to every role in a workspace, so their
events go to the whole room. Chat sessions belong to the user who had them,
so chat events go only to that user's own tabs. Pending invitations are
readable only by roles that may invite, so their events are gated on that
permission and carry nothing but an id. The delivery layer enforces all of
this; publishers only declare it.
"""

from __future__ import annotations

import asyncio

from app.core.logging import get_logger

from .broker import get_broker, publish_from_worker
from .events import Event, EventType, RowId, make_event

logger = get_logger("realtime.notify")

#: How long a synchronous caller waits for the event loop to accept an event.
#: Short: this is a local hand-off, and a caller doing real work should not
#: be held up by a notification under any circumstances.
HANDOFF_TIMEOUT_SECONDS = 2.0


async def emit(event: Event) -> None:
    """Publish from async code. Never raises."""
    try:
        await get_broker().publish(event)
    except Exception as exc:
        logger.warning(f"Could not publish {event.type}: {exc}")


def emit_threadsafe(event: Event) -> bool:
    """Publish from a thread that is not the event loop. Never raises.

    Two callers land here, and they need different things:

    * A **Celery worker**, which is a separate process with no event loop and
      no sockets. Redis is the only way out, and there is always a Redis when
      there is a worker -- Celery needs a broker to exist at all.
    * **Inline ingestion**, which runs in a worker thread of the API process
      when no broker is configured. There is no Redis, but there *is* a
      running loop with connected clients on it.

    Redis is tried first so that the ordinary deployment fans out to every
    instance, and the loop hand-off covers the case where Redis was the thing
    that was missing.
    """
    try:
        if publish_from_worker(event):
            return True
    except Exception as exc:  # pragma: no cover - publish_from_worker catches
        logger.warning(f"Could not publish {event.type} through Redis: {exc}")

    broker = get_broker()
    loop = broker.loop
    if loop is None or not loop.is_running():
        # A worker process with no Redis: nothing to deliver to, and nothing
        # that could have been delivered to. The client polls the status
        # endpoint, which still works.
        return False

    try:
        future = asyncio.run_coroutine_threadsafe(broker.publish(event), loop)
        future.result(timeout=HANDOFF_TIMEOUT_SECONDS)
        return True
    except Exception as exc:
        logger.warning(f"Could not hand {event.type} to the event loop: {exc}")
        return False


# ---------------------------------------------------------------- documents --
def document_payload(document, uploaded_by: RowId | None = None) -> dict:
    """The shape the documents API returns, so a client can insert it directly.

    Every field the list renders: a payload missing one would show as a blank
    in the row until the next refetch.
    """
    return {
        "id": document.id,
        "title": document.original_filename,
        "fileType": document.file_type,
        "fileSizeKb": (document.file_size or 0) // 1024,
        "status": document.status,
        "chunkCount": document.chunk_count or 0,
        "progress": document.progress or 0,
        "folderPath": document.folder_path,
        "tags": document.tags or [],
        "uploadedBy": uploaded_by or "",
        "uploadedAt": document.created_at.isoformat() if document.created_at else None,
        "version": document.version or 1,
        "ocrApplied": bool(document.ocr_applied),
    }


async def document_created(
    document, actor_id: RowId | None = None, uploaded_by: RowId | None = None
) -> None:
    await emit(
        make_event(
            EventType.DOCUMENT_CREATED,
            document.workspace_id,
            document_payload(document, uploaded_by),
            actor_id=actor_id,
        )
    )


async def document_deleted(workspace_id: RowId, document_id: RowId, actor_id: RowId | None = None):
    await emit(
        make_event(
            EventType.DOCUMENT_DELETED,
            workspace_id,
            {"id": document_id},
            actor_id=actor_id,
        )
    )


def document_status_changed(document) -> bool:
    """Indexing progress, published from whichever process is doing the work.

    Synchronous because the caller is: this is the ingestion pipeline, and it
    runs either in a Celery worker or in a thread of the API process.
    """
    return emit_threadsafe(
        make_event(
            EventType.DOCUMENT_STATUS,
            document.workspace_id,
            {
                "id": document.id,
                "status": document.status,
                "progress": document.progress or 0,
                "chunkCount": document.chunk_count or 0,
                "error": document.error_message,
            },
        )
    )


# ------------------------------------------------------------- collaboration --
def member_payload(member, user=None) -> dict:
    return {
        "id": member.id,
        "userId": member.user_id,
        "role": member.role,
        "name": getattr(user, "full_name", None) or "",
        "email": getattr(user, "email", None) or "",
    }


async def member_added(workspace_id: RowId, member, user=None, actor_id: RowId | None = None):
    await emit(
        make_event(
            EventType.MEMBER_ADDED, workspace_id, member_payload(member, user), actor_id=actor_id
        )
    )


async def member_removed(
    workspace_id: RowId, member_id: RowId, user_id: RowId, actor_id: RowId | None = None
):
    await emit(
        make_event(
            EventType.MEMBER_REMOVED,
            workspace_id,
            {"id": member_id, "userId": user_id},
            actor_id=actor_id,
        )
    )


async def member_role_changed(workspace_id: RowId, member, actor_id: RowId | None = None):
    await emit(
        make_event(
            EventType.MEMBER_ROLE_CHANGED,
            workspace_id,
            {"id": member.id, "userId": member.user_id, "role": member.role},
            actor_id=actor_id,
        )
    )


#: Who may see pending invitations -- the permission GET /invitations checks.
INVITATIONS_PERMISSION = "members.invite"


def _invitation_event(
    event_type: str, workspace_id: RowId, invitation_id: RowId, actor_id: RowId | None = None
):
    """An invitation change, for the members who could list invitations.

    An id and nothing else. Never the token, which is a credential issued to
    one address; and not the email or role either, so that a socket whose
    role changed between two events cannot learn more than an id. The client
    refetches the list, which re-applies authorization at the moment it reads.
    """
    return make_event(
        event_type,
        workspace_id,
        {"id": invitation_id},
        actor_id=actor_id,
        permission=INVITATIONS_PERMISSION,
    )


async def invitation_sent(workspace_id: RowId, invitation, actor_id: RowId | None = None):
    await emit(_invitation_event(EventType.INVITATION_SENT, workspace_id, invitation.id, actor_id))


async def invitation_revoked(
    workspace_id: RowId, invitation_id: RowId, actor_id: RowId | None = None
):
    await emit(
        _invitation_event(EventType.INVITATION_REVOKED, workspace_id, invitation_id, actor_id)
    )


async def invitation_accepted(workspace_id: RowId, invitation_id: RowId, user_id: RowId):
    await emit(
        _invitation_event(EventType.INVITATION_ACCEPTED, workspace_id, invitation_id, user_id)
    )


# -------------------------------------------------------------------- chat --
# Every chat event is addressed to the session's owner, and only to them. A
# session belongs to the person who had it -- GET /chat/sessions filters on
# user_id -- and a title is the opening words of their prompt. What these
# events buy is the owner's *other* tabs staying current.


async def chat_created(workspace_id: RowId, session, actor_id: RowId | None = None):
    await emit(
        make_event(
            EventType.CHAT_CREATED,
            workspace_id,
            {"id": session.id, "title": session.title},
            actor_id=actor_id,
            recipient_id=session.user_id,
        )
    )


async def chat_renamed(session, actor_id: RowId | None = None):
    await emit(
        make_event(
            EventType.CHAT_RENAMED,
            session.workspace_id,
            {"id": session.id, "title": session.title},
            actor_id=actor_id,
            recipient_id=session.user_id,
        )
    )


async def chat_deleted(
    workspace_id: RowId, session_id: RowId, owner_id: RowId, actor_id: RowId | None = None
):
    await emit(
        make_event(
            EventType.CHAT_DELETED,
            workspace_id,
            {"id": session_id},
            actor_id=actor_id,
            recipient_id=owner_id,
        )
    )


async def chat_message(session, message, actor_id: RowId | None = None):
    """A message was persisted.

    Not the token stream: streaming stays on the existing SSE endpoint,
    which is already built for it and answers the one tab that asked. This
    tells the owner's other tabs that the conversation moved on, so they
    refetch it rather than show a stale transcript.
    """
    await emit(
        make_event(
            EventType.CHAT_MESSAGE,
            session.workspace_id,
            {
                "sessionId": session.id,
                "id": getattr(message, "id", None),
                "role": getattr(message, "role", None),
            },
            actor_id=actor_id,
            recipient_id=session.user_id,
        )
    )


__all__ = [
    "chat_created",
    "chat_deleted",
    "chat_message",
    "chat_renamed",
    "document_created",
    "document_deleted",
    "document_status_changed",
    "emit",
    "emit_threadsafe",
    "invitation_accepted",
    "invitation_revoked",
    "invitation_sent",
    "member_added",
    "member_removed",
    "member_role_changed",
]
