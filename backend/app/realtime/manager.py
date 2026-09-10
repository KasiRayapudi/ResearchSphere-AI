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
from dataclasses import dataclass, field

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
    websocket: object
    user_id: str
    workspace_id: str
    #: Highest sequence number handed to this socket, so a resume knows where
    #: the client actually got to rather than where the server thinks it did.
    last_seq: int = 0
    last_seen_ms: int = field(default_factory=monotonic_ms)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    writer: asyncio.Task | None = None
    closing: bool = False

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
        }

    # ------------------------------------------------------------ lifecycle --
    async def start(self) -> None:
        """Begin reaping connections that have stopped answering."""
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.create_task(self._reap_forever())

    async def stop(self) -> None:
        """Close every connection and stop the reaper.

        Called on shutdown so clients are told to go away rather than
        discovering it through a broken pipe: a clean close is what makes
        their reconnect immediate instead of waiting for a timeout.
        """
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
            self._heartbeat = None

        async with self._lock:
            connections = list(self._by_id.values())
        for connection in connections:
            await self._close(connection, CLOSE_GOING_AWAY, "server shutting down")

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
            targets = list(self._rooms.get(event.workspace_id, ()))

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
        while True:
            try:
                await asyncio.sleep(interval)
                await self.reap_stale()
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

    # -------------------------------------------------------------- queries --
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
