"""
The WebSocket endpoint.

One socket per workspace. The client authenticates, joins a room, and from
then on receives what happens in that workspace until it disconnects.

**Authentication travels in the subprotocol, not the query string.** A
browser cannot set headers on a WebSocket, so the usual options are a token
in the URL or a token in ``Sec-WebSocket-Protocol``. The URL is the wrong
place: query strings are written to access logs, proxy logs and browser
history, and the token is a bearer credential. The client therefore offers
``["bearer", "<jwt>"]`` and the server echoes back ``bearer``.

**Nothing about the HTTP stack applies here.** Every BaseHTTPMiddleware in
the application returns early for non-HTTP scopes, so this endpoint sees no
rate limiting, no request id, no CORS and no security headers. Connection
limits are enforced by the manager, and Origin is checked here, because
otherwise neither happens at all.

**The socket is downstream only.** Clients may ping and may ask to resume;
they may not change anything. State changes go through the REST API, which
owns authorization, validation and auditing. A second, weaker front door is
not a feature.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import settings
from app.core.logging import get_logger
from app.realtime.broker import get_broker
from app.realtime.events import PROTOCOL_VERSION, Event, EventType, make_event
from app.realtime.manager import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    Connection,
    ConnectionRefused,
    get_manager,
    may_receive,
)

router = APIRouter()
logger = get_logger("realtime.endpoint")

#: The subprotocol a client offers alongside its token.
BEARER_SUBPROTOCOL = "bearer"

#: Longest client frame accepted. Clients send ping and resume, both tiny;
#: anything larger is a bug or an attempt to make the server allocate.
MAX_FRAME_BYTES = 4096


def _extract_token(websocket: WebSocket) -> str | None:
    """The JWT a client offered, or None.

    Looks only at the subprotocol list. Deliberately not the query string:
    putting a bearer token there leaks it into every log that records a URL.
    """
    offered = websocket.scope.get("subprotocols") or []
    if len(offered) >= 2 and offered[0] == BEARER_SUBPROTOCOL:
        return offered[1]
    return None


def _origin_allowed(websocket: WebSocket) -> bool:
    """Whether the connecting page is one we serve.

    Browsers do not apply CORS to WebSockets, so this is the only place the
    check can happen. It is defence in depth rather than the primary control:
    the token lives in localStorage, which a hostile origin cannot read, so
    it cannot authenticate as the user in the first place. A request with no
    Origin at all is not from a browser -- a CLI, a test, a server -- and is
    allowed, because those are not the threat this addresses.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True

    allowed = settings.cors_origins
    if "*" in allowed:
        return True
    if origin in allowed:
        return True

    # Compare host and scheme rather than the raw string, so a trailing
    # slash or a default port does not reject a legitimate origin.
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    for candidate in allowed:
        with contextlib.suppress(ValueError):
            other = urlparse(candidate)
            if (parsed.scheme, parsed.hostname, parsed.port) == (
                other.scheme,
                other.hostname,
                other.port,
            ):
                return True
    return False


def _authenticate(token: str, workspace_id: str):
    """Resolve the user and confirm they belong to the workspace.

    Returns ``(user_id, display_name, role, expires_at, token_id)``; the
    last two let the connection be closed when its token expires or is
    revoked. Raises ConnectionRefused
    otherwise. The role is kept on the connection because some events are
    visible only to roles that could read the same thing over REST.

    The database session is opened and closed here rather than held for the
    life of the socket: a connection can live for hours, and pinning a pool
    connection for that long would exhaust the pool long before it exhausted
    anything else.
    """
    from app.core.database import SessionLocal
    from app.core.security import decode_token
    from app.core.workspace_access import membership_for
    from app.models.user import User

    try:
        payload = decode_token(token)
    except Exception:
        # decode_token raises HTTPException with the specific reason; the
        # socket gets one code, because telling a caller which part of their
        # token was wrong helps nobody but an attacker.
        raise ConnectionRefused(CLOSE_UNAUTHORIZED, "invalid token") from None

    user_id = payload.get("sub")
    if not user_id:
        raise ConnectionRefused(CLOSE_UNAUTHORIZED, "invalid token")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None or not user.is_active:
            raise ConnectionRefused(CLOSE_UNAUTHORIZED, "invalid token")

        # Membership, not merely a valid token: the workspace id arrives from
        # the client and would otherwise be an invitation to subscribe to
        # somebody else's events.
        membership = membership_for(db, workspace_id, user_id)
        if membership is None:
            raise ConnectionRefused(CLOSE_FORBIDDEN, "not a member of this workspace")
        expires_at = payload.get("exp")
        return (
            str(user.id),
            (user.full_name or user.email or ""),
            membership.role,
            float(expires_at) if expires_at is not None else None,
            payload.get("jti"),
        )
    finally:
        db.close()


@router.websocket("/ws")
async def realtime(websocket: WebSocket, workspace_id: str = "") -> None:
    """Live workspace events.

    Connect with ``new WebSocket(url, ["bearer", accessToken])`` and
    ``?workspace_id=<id>``. The workspace is fixed for the life of the
    socket; switching workspaces means a new connection, which keeps
    isolation a property of the connection rather than of mutable state.
    """
    if not settings.WS_ENABLED:
        await websocket.close(code=CLOSE_FORBIDDEN, reason="realtime disabled")
        return

    if not _origin_allowed(websocket):
        logger.warning(f"Rejected WebSocket from origin {websocket.headers.get('origin')!r}")
        await websocket.close(code=CLOSE_FORBIDDEN, reason="origin not allowed")
        return

    token = _extract_token(websocket)
    if not token:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="authentication required")
        return

    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        await websocket.close(code=CLOSE_FORBIDDEN, reason="workspace_id is required")
        return

    from fastapi.concurrency import run_in_threadpool

    try:
        # Blocking work -- a JWT verification and two queries -- kept off the
        # event loop, which is serving every other socket in this process.
        user_id, display_name, role, expires_at, token_id = await run_in_threadpool(
            _authenticate, token, workspace_id
        )
    except ConnectionRefused as refusal:
        await websocket.close(code=refusal.code, reason=refusal.reason)
        return

    # Echo the subprotocol back. A browser closes the connection if the
    # server selects one it did not offer, or none at all when it offered.
    await websocket.accept(subprotocol=BEARER_SUBPROTOCOL)

    manager = get_manager()
    broker = get_broker()
    connection = Connection(
        id=uuid.uuid4().hex,
        websocket=websocket,
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        token_expires_at=expires_at,
        token_id=token_id,
        # This task: shutdown waits on it, so the teardown below finishes
        # while the broker it publishes through is still running.
        handler=asyncio.current_task(),
    )

    try:
        await manager.register(connection)
    except ConnectionRefused as refusal:
        await websocket.close(code=refusal.code, reason=refusal.reason)
        return

    logger.info(
        f"WebSocket open: user={user_id} workspace={workspace_id} conn={connection.id}",
        extra={"user_id": user_id, "action": "realtime.connect", "resource_type": "workspace"},
    )

    try:
        await _announce(broker, manager, connection, display_name)
        await _serve(broker, manager, connection, websocket)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        # The manager ends the handler of a connection it closed itself,
        # rather than leaving it waiting on a client that may never answer.
        # Any other cancellation -- the server shutting down -- is not ours
        # to swallow.
        if not connection.closing:
            raise
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"WebSocket {connection.id} failed: {exc}")
    finally:
        await _farewell(broker, manager, connection)


async def _announce(broker, manager, connection: Connection, display_name: str) -> None:
    """Tell the client it is ready, and the workspace that it arrived."""
    presence = broker.presence
    newly_online = await presence.arrive(connection.workspace_id, connection.user_id, connection.id)
    online = await presence.online(connection.workspace_id)
    # Where the workspace's sequence stands right now. A socket that then
    # receives nothing still knows what to resume from, and the epoch tells a
    # returning client whether its remembered position means anything here.
    seq, epoch = await broker.current_position(connection.workspace_id)

    await manager.send_to(
        connection,
        Event(
            type=EventType.CONNECTED,
            workspace_id=connection.workspace_id,
            data={
                "connectionId": connection.id,
                "userId": connection.user_id,
                "protocol": PROTOCOL_VERSION,
                "heartbeatSeconds": settings.WS_HEARTBEAT_INTERVAL_SECONDS,
                "online": online,
                "seq": seq,
                "epoch": epoch,
            },
        ),
    )

    if newly_online:
        # Only a genuine arrival is announced. A second tab is the same
        # person, and telling the room they came online again would make
        # every reconnect look like a join.
        await broker.publish(
            make_event(
                EventType.PRESENCE_ONLINE,
                connection.workspace_id,
                {"userId": connection.user_id, "name": display_name, "online": online},
                actor_id=connection.user_id,
            )
        )


async def _farewell(broker, manager, connection: Connection) -> None:
    """Remove the connection and, if it was the last, announce the departure."""
    await manager.unregister(connection)
    with contextlib.suppress(Exception):
        went_offline = await broker.presence.depart(
            connection.workspace_id, connection.user_id, connection.id
        )
        if went_offline:
            online = await broker.presence.online(connection.workspace_id)
            await broker.publish(
                make_event(
                    EventType.PRESENCE_OFFLINE,
                    connection.workspace_id,
                    {"userId": connection.user_id, "online": online},
                    actor_id=connection.user_id,
                )
            )
    with contextlib.suppress(Exception):
        await connection.websocket.close()
    logger.info(
        f"WebSocket closed: user={connection.user_id} conn={connection.id}",
        extra={
            "user_id": connection.user_id,
            "action": "realtime.disconnect",
            "resource_type": "workspace",
        },
    )


async def _serve(broker, manager, connection: Connection, websocket: WebSocket) -> None:
    """Read client frames until the socket closes.

    The reader exists for the heartbeat and for resume; the writing is done
    by the manager's per-connection task, so a client that never sends
    anything still receives everything.
    """
    while True:
        raw = await websocket.receive_text()
        if len(raw) > MAX_FRAME_BYTES:
            await manager.send_to(
                connection,
                Event(
                    type=EventType.ERROR,
                    workspace_id=connection.workspace_id,
                    data={"message": "frame too large"},
                ),
            )
            continue

        try:
            frame = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(frame, dict):
            continue

        # Any frame proves the peer is alive, which is all the heartbeat
        # needs; the reaper closes connections that stop saying anything.
        connection.touch()
        await _handle_frame(broker, manager, connection, frame)
        # Handling a frame can close this connection -- a resume that
        # overflows its own queue. Nothing more will be written to it, so
        # stop reading from it.
        if connection.closing:
            return


async def _handle_frame(broker, manager, connection: Connection, frame: dict) -> None:
    kind = str(frame.get("type") or "")

    if kind == "ping":
        await broker.presence.refresh(connection.workspace_id, connection.user_id)
        await manager.send_to(
            connection,
            Event(
                type=EventType.PONG,
                workspace_id=connection.workspace_id,
                # Echoed so the client can measure its own round trip
                # rather than guessing at it.
                data={"t": frame.get("t")},
            ),
        )
        return

    if kind == "resume":
        await _resume(broker, manager, connection, frame)
        return

    # Unknown frames are ignored rather than answered. A client that sends
    # something this server does not understand is a client from another
    # version; an error reply would only fill its console.


async def _resume(broker, manager, connection: Connection, frame: dict) -> None:
    """Replay what a reconnecting client missed, and say whether that was all.

    The answer ends with a ``connection.resumed`` frame whose ``complete``
    flag is the only thing a client should trust. It cannot infer loss from
    gaps in the sequence: events restricted to other users or other roles
    leave gaps in what any one client sees, by design.
    """
    try:
        after = max(0, int(frame.get("after") or 0))
    except (TypeError, ValueError):
        after = 0
    workspace_id = connection.workspace_id
    seq, epoch = await broker.current_position(workspace_id)

    if str(frame.get("epoch") or "") != epoch:
        # A different sequence space: the counter restarted since the client
        # last saw it, so its position is meaningless here and replaying
        # "after" it would either skip or duplicate. It must refetch.
        replayed, complete = 0, False
    elif after >= seq:
        replayed, complete = 0, True
    else:
        missed = await broker.replay(workspace_id, after_seq=after, epoch=epoch)
        oldest = await broker.oldest_retained(workspace_id)
        # Complete only if the log still reaches back to the event right
        # after the client's position. Otherwise some were trimmed away.
        complete = oldest != 0 and oldest <= after + 1
        replayed = 0
        for event in missed:
            # The same predicate as live delivery, so a restricted event
            # cannot reach someone through replay that it never reached live.
            if not may_receive(connection, event):
                continue
            if not await manager.send_to(connection, event):
                return
            replayed += 1

    await manager.send_to(
        connection,
        Event(
            type=EventType.RESUMED,
            workspace_id=workspace_id,
            data={
                "after": after,
                "replayed": replayed,
                "complete": complete,
                "seq": seq,
                "epoch": epoch,
            },
        ),
    )
    logger.debug(
        f"Resumed {connection.id} from seq {after}: {replayed} replayed, complete={complete}"
    )
