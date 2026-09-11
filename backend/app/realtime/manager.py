"""
WebSocket connection manager.

Holds the sockets this process is serving, grouped into one room per
workspace, and fans events out to them.

Three decisions shape everything here.

**Each connection owns a queue and a writer task.** Broadcasting by awaiting
``send_json`` in a loop makes the slowest client the speed of the broadcast:
one browser on a bad connection stalls delivery to everyone else in the
workspace. Publishing therefore only enqueues, which never blocks, and a
per-connection task does the writing.

**A queue that fills is a disconnect, not a backlog.** A client that cannot
keep up with its own workspace's events is not going to catch up, and
buffering for it costs memory that healthy connections need. It is closed and
told to reconnect, which is a path that already exists and already replays
what it missed.

**Limits live here.** Every BaseHTTPMiddleware in the stack -- rate limiting
included -- returns early for non-HTTP scopes, so a WebSocket reaches this
code having passed through none of them. If connections are not capped here
they are not capped anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import settings
from app.core.logging import get_logger

from .events import Event, EventType, monotonic_ms

logger = get_logger("realtime.manager")


class ConnectionRefused(Exception):
    """A connection was rejected before it joined a room."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


#: Close codes. 4000+ is the range reserved for application use, so these
#: never collide with protocol codes and a client can tell them apart.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_TOO_MANY = 4429
CLOSE_SLOW_CONSUMER = 4408
CLOSE_TIMEOUT = 4400
CLOSE_GOING_AWAY = 1001

#: How long shutdown waits for connection handlers to finish their own
#: teardown -- presence cleanup, the offline announcement -- before giving up
#: on them. Bounded, because a peer that never answers the close must not be
#: able to hold the process open.
SHUTDOWN_GRACE_SECONDS = 5.0


def may_receive(connection: Connection, event: Event) -> bool:
    """Whether a socket is entitled to an event. Fails closed.

    The single place audience rules live. Live delivery and reconnect replay
    both call it, so a restricted event cannot reach someone through the
    replay path that the live path would have refused.
    """
    if event.workspace_id != connection.workspace_id:
        return False
    if event.recipient_id is not None and event.recipient_id != connection.user_id:
        return False
    if event.permission is not None:
        # Imported here: workspace_access pulls in the ORM models, which the
        # event layer should not need just to be imported.
        from app.core.workspace_access import can

        # can() fails closed on an unknown permission or a missing role.
        return can(connection.role, event.permission)
    return True


class SocketLike(Protocol):
    """What the manager needs from a socket: to write to it and to close it.

    Narrower than Starlette's WebSocket on purpose. Reading belongs to the
    endpoint, and a manager that only writes can be driven by anything that
    accepts frames.
    """

    async def send_json(self, data: Any) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


@dataclass(eq=False)
class Connection:
    """One live socket.

    ``workspace_id`` is fixed for the lifetime of the connection. Switching
    workspaces means a new socket -- it makes isolation a property of the
    connection rather than of mutable state that a bug could desynchronise.

    ``eq=False`` keeps identity equality and therefore hashability, which
    rooms depend on: two connections are the same only if they are the same
    socket, and a generated __eq__ would both break the sets and make two
    distinct tabs of one user compare equal.
    """

    id: str
    websocket: SocketLike
    user_id: str
    workspace_id: str
    #: Highest sequence number handed to this socket, so a resume knows where
    #: the client actually got to rather than where the server thinks it did.
    last_seq: int = 0
    last_seen_ms: int = field(default_factory=monotonic_ms)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    writer: asyncio.Task | None = None
    closing: bool = False
    #: The member's role when the socket opened, kept current by
    #: member.role_changed events. Decides permission-gated delivery.
    role: str | None = None
    #: The task serving this socket, when there is one. Shutdown waits on it
    #: so teardown finishes before the broker goes away, and it is what lets
    #: a caller observe that a connection has genuinely finished.
    handler: asyncio.Task | None = None
    #: When the token this socket authenticated with expires (Unix seconds).
    #: The socket is closed then, with 4401, and the client reconnects with a
    #: fresh token: a connection may not outlive its credential.
    token_expires_at: float | None = None
    #: The token's jti, so a revocation reaches sockets as well as requests.
    token_id: str | None = None

    def touch(self) -> None:
        self.last_seen_ms = monotonic_ms()


class ConnectionManager:
    """The sockets served by this process.

    One instance per process, reached through :func:`get_manager`. Not a
    module-level dict: the lifecycle (start the heartbeat, drain on shutdown)
    needs somewhere to live, and tests need to be able to build a clean one.
    """

    def __init__(
        self,
        *,
        max_per_user: int | None = None,
        max_total: int | None = None,
        queue_size: int | None = None,
    ):
        self._rooms: dict[str, set[Connection]] = {}
        self._by_id: dict[str, Connection] = {}
        self._lock = asyncio.Lock()
        self._heartbeat: asyncio.Task | None = None
        #: The loop that started this manager. Its tasks and queues belong to
        #: that loop, so only that loop may stop them.
        self._loop: asyncio.AbstractEventLoop | None = None
        #: Called by the reaper every WS_REVALIDATE_SECONDS with the manager;
        #: see app/realtime/revalidation.py. Optional so the manager stays
        #: usable without a database.
        self._validator = None
        self.max_per_user = max_per_user or settings.WS_MAX_CONNECTIONS_PER_USER
        self.max_total = max_total or settings.WS_MAX_CONNECTIONS_TOTAL
        self.queue_size = queue_size or settings.WS_SEND_QUEUE_SIZE
        #: Counters for the metrics endpoint and for tests that need to prove
        #: a disconnect happened for the reason they expected.
        self.stats = {
            "accepted": 0,
            "rejected_limit": 0,
            "closed_slow": 0,
            "closed_timeout": 0,
            "dropped_isolation": 0,
            "filtered": 0,
            "revoked": 0,
        }

    # ------------------------------------------------------------ lifecycle --
    async def start(self, validator=None) -> None:
        """Begin reaping connections that have stopped answering.

        ``validator``, when given, is awaited with the manager every
        WS_REVALIDATE_SECONDS to close sockets whose authorization has lapsed.

        A manager already running on another, live event loop is left alone:
        its tasks and queues belong to that loop, and a second lifespan in
        the same process -- another app instance, or a test -- must not take
        them over. If the previous owner's loop has died, this loop takes
        over and anything bound to the dead loop is discarded.
        """
        current = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not current:
            if not self._loop.is_closed() and self._loop.is_running():
                logger.warning(
                    "Realtime manager is already running on another event loop; " "leaving it there"
                )
                return
            self._heartbeat = None
            self._rooms.clear()
            self._by_id.clear()
        self._loop = current
        if validator is not None:
            self._validator = validator
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.create_task(self._reap_forever())

    async def stop(self) -> None:
        """Close every connection and stop the reaper.

        Called on shutdown so clients are told to go away rather than
        discovering it through a broken pipe: a clean close is what makes
        their reconnect immediate instead of waiting for a timeout.

        Only the loop that started the manager can stop it. From any other
        loop this does nothing: cancelling another loop's tasks from the
        wrong thread is unsafe, and would tear down sockets that loop is
        still serving.
        """
        if self._loop is not None and self._loop is not asyncio.get_running_loop():
            return
        self._loop = None
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
            self._heartbeat = None

        async with self._lock:
            connections = list(self._by_id.values())
        for connection in connections:
            await self._close(connection, CLOSE_GOING_AWAY, "server shutting down")

        # Closing a socket only starts its teardown; the handler still has
        # presence to clear and a departure to announce. Waiting here is what
        # lets the caller stop the broker afterwards without that work
        # reopening a Redis connection nobody will close.
        handlers = [
            c.handler for c in connections if c.handler is not None and not c.handler.done()
        ]
        if handlers:
            _, pending = await asyncio.wait(handlers, timeout=SHUTDOWN_GRACE_SECONDS)
            if pending:
                logger.warning(
                    f"{len(pending)} WebSocket handler(s) did not finish within "
                    f"{SHUTDOWN_GRACE_SECONDS}s of shutdown"
                )

    # ----------------------------------------------------------- membership --
    async def register(self, connection: Connection) -> None:
        """Add an accepted socket to its workspace room.

        Limits are checked here rather than at the endpoint so there is one
        place that can say no, and so the check and the insertion happen
        under the same lock -- otherwise two simultaneous connections both
        see room and both get in.
        """
        connection.queue = asyncio.Queue(maxsize=self.queue_size)

        async with self._lock:
            if len(self._by_id) >= self.max_total:
                self.stats["rejected_limit"] += 1
                raise ConnectionRefused(CLOSE_TOO_MANY, "server connection limit reached")

            for_user = sum(1 for c in self._by_id.values() if c.user_id == connection.user_id)
            if for_user >= self.max_per_user:
                self.stats["rejected_limit"] += 1
                raise ConnectionRefused(
                    CLOSE_TOO_MANY,
                    f"at most {self.max_per_user} simultaneous connections per user",
                )

            self._rooms.setdefault(connection.workspace_id, set()).add(connection)
            self._by_id[connection.id] = connection
            self.stats["accepted"] += 1

        connection.writer = asyncio.create_task(self._drain(connection))

    async def unregister(self, connection: Connection) -> None:
        """Remove a socket and stop its writer. Safe to call twice."""
        async with self._lock:
            room = self._rooms.get(connection.workspace_id)
            if room is not None:
                room.discard(connection)
                if not room:
                    # Empty rooms are removed rather than left behind: a
                    # long-lived process would otherwise accumulate one entry
                    # per workspace ever visited.
                    self._rooms.pop(connection.workspace_id, None)
            self._by_id.pop(connection.id, None)

        writer = connection.writer
        connection.writer = None
        if writer is not None and not writer.done():
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer

    # ------------------------------------------------------------ delivery --
    async def publish(self, event: Event) -> int:
        """Fan an event out to its workspace. Returns how many sockets took it.

        The workspace on the event decides who receives it. A connection in
        another room is never a candidate, so a publisher cannot leak across
        tenants by forgetting to filter.
        """
        if not event.workspace_id:
            self.stats["dropped_isolation"] += 1
            logger.error(f"Refusing to publish {event.type} with no workspace")
            return 0

        async with self._lock:
            room = list(self._rooms.get(event.workspace_id, ()))

        # Membership changes are applied to live sockets here, where every
        # instance sees them -- locally published or arriving over Redis.
        # Authorization was checked when the socket opened; without this, a
        # demoted member would keep their old role and a removed one would
        # keep receiving the workspace until they happened to reconnect.
        subject = str((event.data or {}).get("userId") or "")
        if event.type == EventType.MEMBER_ROLE_CHANGED and subject:
            for connection in room:
                if connection.user_id == subject:
                    connection.role = event.data.get("role")

        targets = [c for c in room if may_receive(c, event)]
        self.stats["filtered"] += len(room) - len(targets)

        payload = event.to_wire()
        delivered = 0
        overloaded: list[Connection] = []
        for connection in targets:
            if connection.closing:
                continue
            try:
                connection.queue.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:
                overloaded.append(connection)

        for connection in overloaded:
            self.stats["closed_slow"] += 1
            logger.warning(
                f"Dropping slow connection {connection.id} "
                f"(user={connection.user_id}, queue full at {self.queue_size})"
            )
            await self._close(connection, CLOSE_SLOW_CONSUMER, "client too slow")

        if event.type == EventType.MEMBER_REMOVED and subject:
            for connection in room:
                if connection.user_id == subject and not connection.closing:
                    self.stats["revoked"] += 1
                    await self._close(connection, CLOSE_FORBIDDEN, "membership revoked")

        return delivered

    async def send_to(self, connection: Connection, event: Event) -> bool:
        """Send one event to one socket, for connection-level frames."""
        if connection.closing:
            return False
        try:
            connection.queue.put_nowait(event.to_wire())
            return True
        except asyncio.QueueFull:
            await self._close(connection, CLOSE_SLOW_CONSUMER, "client too slow")
            return False

    async def _drain(self, connection: Connection) -> None:
        """Write queued events to one socket until it dies."""
        websocket = connection.websocket
        try:
            while True:
                payload = await connection.queue.get()
                await websocket.send_json(payload)
                seq = payload.get("seq") or 0
                if seq > connection.last_seq:
                    connection.last_seq = seq
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A write failure means the socket is gone. The reader will
            # notice too, but not necessarily soon, so mark it here.
            if not connection.closing:
                logger.info(f"Connection {connection.id} write failed: {exc}")
                connection.closing = True

    # ------------------------------------------------------------ heartbeat --
    async def _reap_forever(self) -> None:
        interval = max(1, settings.WS_HEARTBEAT_INTERVAL_SECONDS)
        last_validated = time.monotonic()
        while True:
            try:
                await asyncio.sleep(interval)
                await self.reap_stale()
                due = max(interval, settings.WS_REVALIDATE_SECONDS)
                if self._validator is not None and time.monotonic() - last_validated >= due:
                    last_validated = time.monotonic()
                    await self._validator(self)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(f"Heartbeat sweep failed: {exc}")

    async def reap_stale(self) -> int:
        """Close connections that have not been heard from. Returns the count.

        A TCP connection can stay open long after the peer is gone -- a
        laptop closing its lid, a NAT dropping the mapping. Without this the
        room keeps a socket nobody is reading, and presence keeps reporting a
        user who left.
        """
        deadline = monotonic_ms() - max(1, settings.WS_HEARTBEAT_TIMEOUT_SECONDS) * 1000
        async with self._lock:
            stale = [c for c in self._by_id.values() if c.last_seen_ms < deadline]

        for connection in stale:
            self.stats["closed_timeout"] += 1
            logger.info(f"Reaping silent connection {connection.id} (user={connection.user_id})")
            await self._close(connection, CLOSE_TIMEOUT, "heartbeat timeout")
        return len(stale)

    async def _close(self, connection: Connection, code: int, reason: str) -> None:
        """Take a connection out of service, whatever state it is in."""
        if connection.closing:
            return
        connection.closing = True
        await self.unregister(connection)
        with contextlib.suppress(Exception):
            await connection.websocket.close(code=code, reason=reason)

        # The server has decided this connection is over. Its handler must not
        # then wait for the client to finish the close handshake: a peer the
        # reaper gave up on is gone and will never answer, and until the
        # handler returns, that user stays "online". The endpoint treats this
        # cancellation as a normal end and tears down in its finally block.
        handler = connection.handler
        if handler is not None and not handler.done() and handler is not asyncio.current_task():
            handler.cancel()

    # -------------------------------------------------------------- queries --
    def live(self) -> list[Connection]:
        """Every connection currently in service."""
        return [c for c in self._by_id.values() if not c.closing]

    async def close(self, connection: Connection, code: int, reason: str) -> None:
        """Take one connection out of service with the given close code."""
        await self._close(connection, code, reason)

    def get(self, connection_id: str) -> Connection | None:
        return self._by_id.get(connection_id)

    @staticmethod
    async def wait_for_teardown(
        connection: Connection, timeout: float = SHUTDOWN_GRACE_SECONDS
    ) -> bool:
        """Wait until the task serving a connection has finished. True if it did.

        A client closing its end does not wait for the server to finish
        cleaning up, so anything that needs the cleanup to have happened --
        shutdown ordering, or a test asserting on it -- waits on the handler
        itself rather than on the clock.
        """
        handler = connection.handler
        if handler is None or handler.done():
            return True
        done, _ = await asyncio.wait({handler}, timeout=timeout)
        return bool(done)

    def room_size(self, workspace_id: str) -> int:
        return len(self._rooms.get(workspace_id, ()))

    def total_connections(self) -> int:
        return len(self._by_id)

    def workspaces(self) -> list[str]:
        return list(self._rooms)

    def users_in(self, workspace_id: str) -> set[str]:
        return {c.user_id for c in self._rooms.get(workspace_id, ())}

    def connections_for(self, user_id: str, workspace_id: str) -> list[Connection]:
        return [c for c in self._rooms.get(workspace_id, ()) if c.user_id == user_id]

    def snapshot(self) -> dict:
        """Operator-facing state, for the health endpoint."""
        return {
            "connections": self.total_connections(),
            "workspaces": len(self._rooms),
            "limits": {"per_user": self.max_per_user, "total": self.max_total},
            "stats": dict(self.stats),
        }


_manager: ConnectionManager | None = None


def get_manager() -> ConnectionManager:
    """The manager for this process."""
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager


def reset_manager() -> None:
    """Drop the process manager. For tests and for a clean restart."""
    global _manager
    _manager = None


__all__ = [
    "SHUTDOWN_GRACE_SECONDS",
    "may_receive",
    "CLOSE_FORBIDDEN",
    "CLOSE_GOING_AWAY",
    "CLOSE_SLOW_CONSUMER",
    "CLOSE_TIMEOUT",
    "CLOSE_TOO_MANY",
    "CLOSE_UNAUTHORIZED",
    "Connection",
    "ConnectionManager",
    "ConnectionRefused",
    "EventType",
    "get_manager",
    "reset_manager",
]
