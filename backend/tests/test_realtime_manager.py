"""
Connection manager mechanics.

These exercise the manager directly rather than through a socket, because
what is under test is queueing, limits and reaping -- behaviour that is
awkward to provoke through a real connection and easy to assert here. The
socket itself is a sink that records what was written; the *server* is not
faked, and every behavioural guarantee (authentication, isolation, reconnect,
ordering) is proved end to end through the real endpoint in
``test_realtime_ws.py``.
"""

import asyncio

import pytest
import pytest_asyncio

from app.realtime.events import EventType, make_event
from app.realtime.manager import (
    CLOSE_SLOW_CONSUMER,
    CLOSE_TIMEOUT,
    CLOSE_TOO_MANY,
    Connection,
    ConnectionManager,
    ConnectionRefused,
    get_manager,
    reset_manager,
)


class SocketSink:
    """Somewhere for the writer task to write.

    Records frames and close calls. It can be told to block, which is how a
    slow client is simulated without needing a real network.
    """

    def __init__(self, blocked: bool = False):
        self.frames: list[dict] = []
        self.closed_with: tuple[int, str] | None = None
        self._gate = asyncio.Event()
        if not blocked:
            self._gate.set()

    async def send_json(self, payload: dict) -> None:
        await self._gate.wait()
        self.frames.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)

    def unblock(self) -> None:
        self._gate.set()


def build(manager: ConnectionManager, user: str, workspace: str, **kwargs) -> Connection:
    sink = SocketSink(**kwargs)
    return Connection(
        id=f"{user}-{workspace}-{id(sink)}",
        websocket=sink,
        user_id=user,
        workspace_id=workspace,
    )


async def settle() -> None:
    """Let the writer tasks run.

    Delivery is deliberately asynchronous -- publish only enqueues -- so a
    test that asserts on frames has to give the writers a turn first.
    """
    for _ in range(6):
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def manager():
    """A clean manager, shut down afterwards.

    stop() is not optional housekeeping: every registered connection owns a
    writer task, and a task still pending when the test's event loop closes
    surfaces as noise from a completely unrelated test.
    """
    instance = ConnectionManager(max_per_user=3, max_total=5, queue_size=4)
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.mark.asyncio
class TestDelivery:
    async def test_event_reaches_the_workspace(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)

        delivered = await manager.publish(
            make_event(EventType.DOCUMENT_STATUS, "ws-1", {"id": "d1"}).with_sequence(1)
        )
        await settle()

        assert delivered == 1
        assert connection.websocket.frames[0]["type"] == EventType.DOCUMENT_STATUS
        assert connection.websocket.frames[0]["seq"] == 1

    async def test_every_socket_in_the_room_receives_it(self, manager):
        connections = [build(manager, f"u{i}", "ws-1") for i in range(3)]
        for connection in connections:
            await manager.register(connection)

        assert await manager.publish(make_event(EventType.CHAT_CREATED, "ws-1")) == 3
        await settle()
        assert all(len(c.websocket.frames) == 1 for c in connections)

    async def test_another_workspace_receives_nothing(self, manager):
        mine = build(manager, "u1", "ws-1")
        theirs = build(manager, "u2", "ws-2")
        await manager.register(mine)
        await manager.register(theirs)

        await manager.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "secret"}))
        await settle()

        assert len(mine.websocket.frames) == 1
        assert theirs.websocket.frames == [], "an event crossed a workspace boundary"

    async def test_event_without_a_workspace_is_refused(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)

        assert await manager.publish(make_event(EventType.DOCUMENT_CREATED, "")) == 0
        await settle()
        assert connection.websocket.frames == []
        assert manager.stats["dropped_isolation"] == 1

    async def test_delivery_order_is_preserved(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)

        for seq in range(1, 5):
            await manager.publish(
                make_event(EventType.DOCUMENT_STATUS, "ws-1", {"n": seq}).with_sequence(seq)
            )
        await settle()

        assert [f["seq"] for f in connection.websocket.frames] == [1, 2, 3, 4]

    async def test_last_seq_tracks_what_was_actually_written(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        await manager.publish(make_event(EventType.CHAT_MESSAGE, "ws-1").with_sequence(9))
        await settle()
        assert connection.last_seq == 9


@pytest.mark.asyncio
class TestBackpressure:
    async def test_a_client_that_cannot_keep_up_is_closed(self, manager):
        """Buffering forever for one stalled browser costs everyone memory."""
        slow = build(manager, "u1", "ws-1", blocked=True)
        await manager.register(slow)

        # One event is taken by the writer and blocks there; the queue holds
        # the next four; the sixth has nowhere to go.
        for seq in range(1, 8):
            await manager.publish(make_event(EventType.CHAT_MESSAGE, "ws-1").with_sequence(seq))
            await asyncio.sleep(0)

        assert slow.closing is True
        assert slow.websocket.closed_with[0] == CLOSE_SLOW_CONSUMER
        assert manager.stats["closed_slow"] == 1
        assert manager.total_connections() == 0

    async def test_one_slow_client_does_not_stall_the_others(self, manager):
        slow = build(manager, "u1", "ws-1", blocked=True)
        healthy = build(manager, "u2", "ws-1")
        await manager.register(slow)
        await manager.register(healthy)

        for seq in range(1, 4):
            await manager.publish(make_event(EventType.CHAT_MESSAGE, "ws-1").with_sequence(seq))
        await settle()

        assert len(healthy.websocket.frames) == 3
        assert healthy.websocket.closed_with is None
        slow.websocket.unblock()


@pytest.mark.asyncio
class TestLimits:
    async def test_connections_per_user_are_capped(self, manager):
        for _ in range(manager.max_per_user):
            await manager.register(build(manager, "u1", "ws-1"))

        with pytest.raises(ConnectionRefused) as refusal:
            await manager.register(build(manager, "u1", "ws-1"))

        assert refusal.value.code == CLOSE_TOO_MANY
        assert manager.total_connections() == manager.max_per_user

    async def test_the_cap_is_per_user_not_global(self, manager):
        for _ in range(manager.max_per_user):
            await manager.register(build(manager, "u1", "ws-1"))
        # A different user is unaffected by the first user's tabs.
        await manager.register(build(manager, "u2", "ws-1"))
        assert manager.total_connections() == manager.max_per_user + 1

    async def test_the_process_total_is_capped(self, manager):
        for index in range(manager.max_total):
            await manager.register(build(manager, f"user-{index}", "ws-1"))

        with pytest.raises(ConnectionRefused) as refusal:
            await manager.register(build(manager, "one-too-many", "ws-1"))
        assert refusal.value.code == CLOSE_TOO_MANY
        assert manager.stats["rejected_limit"] == 1

    async def test_a_closed_connection_frees_its_slot(self, manager):
        first = build(manager, "u1", "ws-1")
        await manager.register(first)
        for _ in range(manager.max_per_user - 1):
            await manager.register(build(manager, "u1", "ws-1"))

        await manager.unregister(first)
        # The slot is genuinely free, not just accounted as free.
        await manager.register(build(manager, "u1", "ws-1"))
        assert manager.total_connections() == manager.max_per_user


@pytest.mark.asyncio
class TestHeartbeat:
    async def test_a_silent_connection_is_reaped(self, manager, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "WS_HEARTBEAT_TIMEOUT_SECONDS", 1)
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        # Older than the timeout: the peer is gone even though TCP has not
        # noticed, which is exactly the case this exists for.
        connection.last_seen_ms -= 5000

        assert await manager.reap_stale() == 1
        assert connection.websocket.closed_with[0] == CLOSE_TIMEOUT
        assert manager.total_connections() == 0

    async def test_a_recently_seen_connection_survives(self, manager, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "WS_HEARTBEAT_TIMEOUT_SECONDS", 60)
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        connection.touch()

        assert await manager.reap_stale() == 0
        assert manager.total_connections() == 1

    async def test_the_reaper_runs_on_its_own(self, manager, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "WS_HEARTBEAT_INTERVAL_SECONDS", 1)
        monkeypatch.setattr(settings, "WS_HEARTBEAT_TIMEOUT_SECONDS", 1)
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        connection.last_seen_ms -= 10_000

        await manager.start()
        try:
            for _ in range(30):
                await asyncio.sleep(0.05)
                if manager.total_connections() == 0:
                    break
        finally:
            await manager.stop()
        assert manager.total_connections() == 0, "the reaper never ran"


@pytest.mark.asyncio
class TestLifecycle:
    async def test_unregister_is_idempotent(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        await manager.unregister(connection)
        await manager.unregister(connection)
        assert manager.total_connections() == 0

    async def test_an_empty_room_is_removed(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        assert manager.workspaces() == ["ws-1"]
        await manager.unregister(connection)
        assert manager.workspaces() == [], "empty rooms would accumulate forever"

    async def test_shutdown_closes_every_connection(self, manager):
        connections = [build(manager, f"u{i}", f"ws-{i}") for i in range(3)]
        for connection in connections:
            await manager.register(connection)

        await manager.stop()

        assert manager.total_connections() == 0
        # Told to go away, rather than left to discover a broken pipe.
        assert all(c.websocket.closed_with[0] == 1001 for c in connections)

    async def test_writer_task_stops_with_the_connection(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        writer = connection.writer
        await manager.unregister(connection)
        assert writer.done()

    async def test_publishing_to_a_closed_connection_is_harmless(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        await manager.unregister(connection)
        assert await manager.publish(make_event(EventType.CHAT_CREATED, "ws-1")) == 0


class TestProcessManager:
    def test_the_process_shares_one_manager(self):
        reset_manager()
        try:
            assert get_manager() is get_manager()
        finally:
            reset_manager()

    def test_reset_gives_a_fresh_one(self):
        first = get_manager()
        reset_manager()
        assert get_manager() is not first
        reset_manager()

    @pytest.mark.asyncio
    async def test_queries_report_the_room(self):
        instance = ConnectionManager()
        connection = build(instance, "u1", "ws-1")
        await instance.register(connection)
        try:
            assert instance.room_size("ws-1") == 1
            assert instance.room_size("ws-other") == 0
            assert instance.users_in("ws-1") == {"u1"}
            assert len(instance.connections_for("u1", "ws-1")) == 1
            assert instance.snapshot()["connections"] == 1
        finally:
            await instance.stop()
