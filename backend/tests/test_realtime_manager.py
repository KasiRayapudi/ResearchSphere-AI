"""
Connection manager mechanics.

These exercise the manager directly rather than through a socket, because
what is under test is queueing, audience rules, limits and reaping --
behaviour that is awkward to provoke through a real connection and easy to
assert here. The socket is a sink that records what was written; the
*server* is not faked, and every behavioural guarantee is also proved end to
end through the real endpoint in ``test_realtime_ws.py``.

Nothing here waits on the clock. Delivery is asynchronous by design --
publish only enqueues, a writer task sends -- so tests wait on the sink being
written to, and prove absence by what was never enqueued rather than by
waiting a while and seeing nothing.
"""

import asyncio

import pytest
import pytest_asyncio

from app.realtime import manager as manager_module
from app.realtime.events import EventType, make_event
from app.realtime.manager import (
    CLOSE_FORBIDDEN,
    CLOSE_SLOW_CONSUMER,
    CLOSE_TIMEOUT,
    CLOSE_TOO_MANY,
    Connection,
    ConnectionManager,
    ConnectionRefused,
    get_manager,
    may_receive,
    reset_manager,
)

#: Upper bound on any wait. A failure bound, never a synchronisation
#: mechanism: every wait below returns the moment its condition holds.
FAIL_AFTER = 3.0


class SocketSink:
    """Somewhere for the writer task to write.

    Records frames and close calls, and signals both, so a test can wait for
    exactly the thing it expects instead of for an amount of time. It can be
    told to block, which is how a stalled client is simulated.
    """

    def __init__(self, blocked: bool = False):
        self.frames: list[dict] = []
        self.closed_with: tuple[int, str] | None = None
        self.closed = asyncio.Event()
        self._changed = asyncio.Event()
        self._gate = asyncio.Event()
        if not blocked:
            self._gate.set()

    async def send_json(self, payload: dict) -> None:
        await self._gate.wait()
        self.frames.append(payload)
        self._changed.set()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed_with = (code, reason)
        self.closed.set()

    def unblock(self) -> None:
        self._gate.set()

    async def wait_frames(self, count: int, timeout: float = FAIL_AFTER) -> list[dict]:
        """Return once at least ``count`` frames have been written.

        Clearing and waiting happen with no await between the length check
        and clear(), so on a single event loop a write cannot slip between
        them and be missed.
        """

        async def until() -> None:
            while len(self.frames) < count:
                self._changed.clear()
                await self._changed.wait()

        await asyncio.wait_for(until(), timeout)
        return self.frames


def build(
    manager: ConnectionManager, user: str, workspace: str, role: str | None = "editor", **kwargs
) -> Connection:
    sink = SocketSink(**kwargs)
    return Connection(
        id=f"{user}-{workspace}-{id(sink)}",
        websocket=sink,
        user_id=user,
        workspace_id=workspace,
        role=role,
    )


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
        frames = await connection.websocket.wait_frames(1)

        assert delivered == 1
        assert frames[0]["type"] == EventType.DOCUMENT_STATUS
        assert frames[0]["seq"] == 1

    async def test_every_socket_in_the_room_receives_it(self, manager):
        connections = [build(manager, f"u{i}", "ws-1") for i in range(3)]
        for connection in connections:
            await manager.register(connection)

        assert await manager.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1")) == 3
        for connection in connections:
            assert len(await connection.websocket.wait_frames(1)) == 1

    async def test_another_workspace_receives_nothing(self, manager):
        mine = build(manager, "u1", "ws-1")
        theirs = build(manager, "u2", "ws-2")
        await manager.register(mine)
        await manager.register(theirs)

        await manager.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "secret"}))

        # publish() enqueues synchronously for every target, so a socket
        # that was a target has the event in its queue right now. Empty
        # means it was never a candidate -- no waiting required to know.
        assert theirs.queue.empty(), "an event crossed a workspace boundary"
        await mine.websocket.wait_frames(1)
        assert theirs.websocket.frames == []

    async def test_event_without_a_workspace_is_refused(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)

        assert await manager.publish(make_event(EventType.DOCUMENT_CREATED, "")) == 0
        assert connection.queue.empty()
        assert manager.stats["dropped_isolation"] == 1

    async def test_delivery_order_is_preserved(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)

        for seq in range(1, 5):
            await manager.publish(
                make_event(EventType.DOCUMENT_STATUS, "ws-1", {"n": seq}).with_sequence(seq)
            )

        frames = await connection.websocket.wait_frames(4)
        assert [f["seq"] for f in frames] == [1, 2, 3, 4]

    async def test_last_seq_tracks_what_was_actually_written(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        await manager.publish(make_event(EventType.CHAT_MESSAGE, "ws-1").with_sequence(9))
        # The writer records last_seq before it next yields, so by the time
        # this waiter runs the update has happened.
        await connection.websocket.wait_frames(1)
        assert connection.last_seq == 9


@pytest.mark.asyncio
class TestAudience:
    """Not everything in a workspace is visible to everyone in it."""

    async def test_a_recipient_restricted_event_reaches_only_that_user(self, manager):
        owner_tab = build(manager, "owner", "ws-1")
        owner_other_tab = build(manager, "owner", "ws-1")
        colleague = build(manager, "colleague", "ws-1")
        for connection in (owner_tab, owner_other_tab, colleague):
            await manager.register(connection)

        delivered = await manager.publish(
            make_event(
                EventType.CHAT_RENAMED,
                "ws-1",
                {"title": "my private prompt"},
                recipient_id="owner",
            )
        )

        assert delivered == 2, "both of the owner's tabs, and nobody else"
        assert colleague.queue.empty(), "a private chat title reached another member"
        await owner_tab.websocket.wait_frames(1)
        await owner_other_tab.websocket.wait_frames(1)

    @pytest.mark.parametrize(
        "role,expected",
        [("owner", True), ("admin", True), ("editor", False), ("viewer", False), (None, False)],
    )
    async def test_a_permission_gated_event_follows_the_permission_matrix(
        self, manager, role, expected
    ):
        connection = build(manager, "u1", "ws-1", role=role)
        await manager.register(connection)

        await manager.publish(
            make_event(EventType.INVITATION_SENT, "ws-1", {"id": "i1"}, permission="members.invite")
        )
        assert (not connection.queue.empty()) is expected

    async def test_an_unknown_permission_reaches_nobody(self, manager):
        """A typo in a permission name must fail closed, not open."""
        connection = build(manager, "u1", "ws-1", role="owner")
        await manager.register(connection)
        await manager.publish(
            make_event(EventType.INVITATION_SENT, "ws-1", permission="members.invitee")
        )
        assert connection.queue.empty()

    async def test_the_rule_is_one_function(self, manager):
        """Replay calls the same predicate as live delivery."""
        connection = build(manager, "u1", "ws-1", role="viewer")
        private = make_event(EventType.CHAT_CREATED, "ws-1", recipient_id="someone-else")
        gated = make_event(EventType.INVITATION_SENT, "ws-1", permission="members.invite")
        foreign = make_event(EventType.DOCUMENT_CREATED, "ws-2")
        public = make_event(EventType.DOCUMENT_CREATED, "ws-1")

        assert not may_receive(connection, private)
        assert not may_receive(connection, gated)
        assert not may_receive(connection, foreign)
        assert may_receive(connection, public)


@pytest.mark.asyncio
class TestMembershipEnforcement:
    """Authorization is checked when a socket opens; membership can change after."""

    async def test_a_promotion_takes_effect_on_the_open_socket(self, manager):
        connection = build(manager, "u1", "ws-1", role="viewer")
        await manager.register(connection)

        await manager.publish(
            make_event(EventType.MEMBER_ROLE_CHANGED, "ws-1", {"userId": "u1", "role": "admin"})
        )
        assert connection.role == "admin"

        await manager.publish(
            make_event(EventType.INVITATION_SENT, "ws-1", {"id": "i1"}, permission="members.invite")
        )
        frames = await connection.websocket.wait_frames(2)
        assert frames[1]["type"] == EventType.INVITATION_SENT

    async def test_a_demotion_takes_effect_on_the_open_socket(self, manager):
        connection = build(manager, "u1", "ws-1", role="admin")
        await manager.register(connection)

        await manager.publish(
            make_event(EventType.MEMBER_ROLE_CHANGED, "ws-1", {"userId": "u1", "role": "viewer"})
        )
        await connection.websocket.wait_frames(1)
        await manager.publish(
            make_event(EventType.INVITATION_SENT, "ws-1", permission="members.invite")
        )
        assert connection.queue.empty(), "a demoted member kept an admin's view"

    async def test_a_removed_member_is_disconnected(self, manager):
        """Otherwise they would keep receiving the workspace after removal."""
        removed_tabs = [build(manager, "gone", "ws-1") for _ in range(2)]
        colleague = build(manager, "stays", "ws-1")
        for connection in (*removed_tabs, colleague):
            await manager.register(connection)

        await manager.publish(
            make_event(EventType.MEMBER_REMOVED, "ws-1", {"id": "m1", "userId": "gone"})
        )

        for connection in removed_tabs:
            assert connection.websocket.closed_with[0] == CLOSE_FORBIDDEN
        assert manager.connections_for("gone", "ws-1") == []
        assert manager.stats["revoked"] == 2
        # Everyone else is told, and stays connected.
        await colleague.websocket.wait_frames(1)
        assert colleague.websocket.closed_with is None

    async def test_removal_from_one_workspace_leaves_the_others(self, manager):
        elsewhere = build(manager, "gone", "ws-2")
        await manager.register(elsewhere)
        await manager.publish(make_event(EventType.MEMBER_REMOVED, "ws-1", {"userId": "gone"}))
        assert elsewhere.websocket.closed_with is None


@pytest.mark.asyncio
class TestBackpressure:
    async def test_a_client_that_cannot_keep_up_is_closed(self, manager):
        """Buffering forever for one stalled browser costs everyone memory."""
        slow = build(manager, "u1", "ws-1", blocked=True)
        await manager.register(slow)

        # No await between these publishes lets the writer run, so the queue
        # takes exactly queue_size and the next one has nowhere to go. Two
        # over keeps that true even if the writer had taken one first.
        for seq in range(1, manager.queue_size + 3):
            await manager.publish(make_event(EventType.CHAT_MESSAGE, "ws-1").with_sequence(seq))

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

        assert len(await healthy.websocket.wait_frames(3)) == 3
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
        """The background task, not a direct call: waits on the close it causes."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "WS_HEARTBEAT_INTERVAL_SECONDS", 1)
        monkeypatch.setattr(settings, "WS_HEARTBEAT_TIMEOUT_SECONDS", 1)
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        connection.last_seen_ms -= 10_000

        await manager.start()
        # The interval has a one-second floor, so this legitimately takes
        # about a second; the bound is only there so a broken reaper fails.
        await asyncio.wait_for(connection.websocket.closed.wait(), timeout=5)
        assert connection.websocket.closed_with[0] == CLOSE_TIMEOUT
        assert manager.total_connections() == 0


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

    async def test_shutdown_waits_for_handlers_to_finish_their_teardown(self, manager):
        """So the broker is not stopped while handlers still need it."""
        connection = build(manager, "u1", "ws-1")
        finished = []

        async def handler():
            # Shaped like the endpoint's: the manager cancels the handler of a
            # connection it has closed, and teardown runs in the finally.
            try:
                await asyncio.Event().wait()  # the client, which never answers
            except asyncio.CancelledError:
                pass
            finally:
                # Teardown that needs another turn of the loop, as a real one
                # does when it clears presence and announces the departure.
                await asyncio.sleep(0)
                finished.append(True)

        connection.handler = asyncio.create_task(handler())
        await manager.register(connection)

        await manager.stop()
        assert finished == [True], "stop() returned before the handler finished"

    async def test_shutdown_does_not_wait_forever(self, manager, monkeypatch):
        monkeypatch.setattr(manager_module, "SHUTDOWN_GRACE_SECONDS", 0.05)
        connection = build(manager, "u1", "ws-1")
        release = asyncio.Event()

        async def stubborn():
            # Ignores cancellation, as a buggy handler might: the grace
            # period is what stops it holding shutdown open.
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        connection.handler = asyncio.create_task(stubborn())
        await manager.register(connection)
        try:
            await manager.stop()
            assert not connection.handler.done(), "a stuck handler must not hold shutdown"
        finally:
            release.set()
            await connection.handler

    async def test_closing_a_connection_ends_its_handler(self, manager):
        """Without waiting for the client, which may never answer."""
        connection = build(manager, "u1", "ws-1")
        connection.handler = asyncio.create_task(asyncio.Event().wait())
        await manager.register(connection)

        await manager.close(connection, 4400, "heartbeat timeout")
        assert await manager.wait_for_teardown(connection, timeout=FAIL_AFTER) is True
        assert connection.handler.cancelled()

    async def test_teardown_can_be_awaited(self, manager):
        connection = build(manager, "u1", "ws-1")
        release = asyncio.Event()
        connection.handler = asyncio.create_task(release.wait())
        await manager.register(connection)

        waiting = asyncio.create_task(manager.wait_for_teardown(connection))
        await asyncio.sleep(0)
        assert not waiting.done()
        release.set()
        assert await waiting is True

    async def test_teardown_of_a_connection_without_a_handler_is_immediate(self, manager):
        assert await manager.wait_for_teardown(build(manager, "u1", "ws-1")) is True

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

    async def test_a_connection_can_be_found_by_id(self, manager):
        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        assert manager.get(connection.id) is connection
        assert manager.get("nope") is None


class TestLoopOwnership:
    """The manager belongs to the loop that started it."""

    @pytest.mark.asyncio
    async def test_another_loop_cannot_stop_it(self, manager):
        import threading

        connection = build(manager, "u1", "ws-1")
        await manager.register(connection)
        await manager.start()
        heartbeat = manager._heartbeat

        # A second lifespan on its own loop, in another thread -- which is
        # what a nested TestClient or a second app instance amounts to.
        thread = threading.Thread(target=lambda: asyncio.run(manager.stop()))
        thread.start()
        thread.join()

        assert not heartbeat.done(), "another loop cancelled this loop's reaper"
        assert manager.total_connections() == 1
        assert connection.websocket.closed_with is None

    @pytest.mark.asyncio
    async def test_another_loop_does_not_take_over_a_live_owner(self, manager):
        import threading

        await manager.start()
        owner = manager._loop
        thread = threading.Thread(target=lambda: asyncio.run(manager.start()))
        thread.start()
        thread.join()
        assert manager._loop is owner

    def test_a_dead_owner_is_taken_over(self):
        instance = ConnectionManager()

        async def run_and_abandon():
            await instance.start()

        # This loop starts it and then closes without stopping it.
        asyncio.run(run_and_abandon())

        async def take_over():
            await instance.start()
            try:
                assert instance._loop is asyncio.get_running_loop()
                assert not instance._heartbeat.done()
            finally:
                await instance.stop()

        asyncio.run(take_over())


class TestProcessManager:
    """The process-wide manager is shared with the running application.

    Every test that replaces it restores it: replacing it for good would
    leave the app registering sockets on one manager while the broker
    delivered to another.
    """

    def test_the_process_shares_one_manager(self, monkeypatch):
        monkeypatch.setattr(manager_module, "_manager", None)
        assert get_manager() is get_manager()

    def test_reset_gives_a_fresh_one(self, monkeypatch):
        monkeypatch.setattr(manager_module, "_manager", None)
        first = get_manager()
        reset_manager()
        assert get_manager() is not first

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
