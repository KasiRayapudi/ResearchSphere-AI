"""
Cross-instance event delivery, sequencing and presence.

Redis here is fakeredis, which implements the protocol in process rather than
stubbing the calls out: pub/sub really does carry messages between two
clients of the same server, INCR and SET NX really are shared, and LTRIM
really discards the oldest entries. That is what makes a two-broker test
meaningful -- the fan-out logic is exercised, not asserted against a
recording of itself.

What it cannot cover is the wire. fakeredis's connection applies no read
timeout, never loses its socket, and has no server that can stall or drop a
client -- which is what breaks a pub/sub subscriber in production. The same
broker runs against a real Redis in ``test_realtime_redis.py``.

No test waits on the clock. Subscription is awaited through the broker's
``subscribed`` event; arrival through the sink's frame signal; and absence is
proved with a sentinel -- pub/sub delivers one channel in order to one
subscriber, so once a later message has been handled, an earlier one has
been too. The subscriber's own timers are the exception: pings and
reconnection happen on its schedule, not in answer to anything a test does,
so ``until`` watches for their effect, bounded.
"""

import asyncio
import json
import math

import fakeredis
import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.core.config import settings
from app.realtime import broker as broker_module
from app.realtime import manager as manager_module
from app.realtime.broker import (
    CHANNEL,
    EventBroker,
    epoch_key,
    log_key,
    publish_from_worker,
    sequence_key,
)
from app.realtime.events import EventType, make_event
from app.realtime.manager import CLOSE_UNAUTHORIZED, Connection, ConnectionManager
from app.realtime.presence import last_seen_key, presence_key

from .test_realtime_manager import FAIL_AFTER, SocketSink


def connect(manager: ConnectionManager, user: str, workspace: str) -> Connection:
    sink = SocketSink()
    return Connection(id=f"{user}-{id(sink)}", websocket=sink, user_id=user, workspace_id=workspace)


@pytest.fixture
def redis_server():
    """One Redis, shared by every client in a test, as in a deployment."""
    return fakeredis.FakeServer()


def client_for(server) -> "fakeredis.aioredis.FakeRedis":
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


def make_broker(server, instance_id: str) -> EventBroker:
    broker = EventBroker(manager=ConnectionManager(), instance_id=instance_id)
    broker._redis = client_for(server)
    return broker


async def shut(*brokers: EventBroker) -> None:
    for broker in brokers:
        await broker.stop()
        await broker.manager.stop()


async def listening(broker: EventBroker) -> None:
    """Start a broker and return once it is genuinely subscribed."""
    await broker.start()
    await asyncio.wait_for(broker.subscribed.wait(), FAIL_AFTER)


async def until(broker: EventBroker, condition, timeout: float = FAIL_AFTER) -> None:
    """Wait, bounded, for the subscriber to reach a state.

    Wakes on the broker's ``changed`` signal rather than polling: pings and
    reconnection happen on the subscriber's schedule, not in answer to
    anything the test does. Checking and clearing happen with no await
    between them, so a change cannot slip past unseen.
    """

    async def watch() -> None:
        while not condition():
            broker.changed.clear()
            await broker.changed.wait()

    await asyncio.wait_for(watch(), timeout)


async def subscribers(client) -> int:
    """How many subscriptions the server itself holds on the channel."""
    return dict(await client.pubsub_numsub(CHANNEL)).get(CHANNEL, 0)


def running_subscribers() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and getattr(task.get_coro(), "__qualname__", "") == "EventBroker._subscribe_forever"
    ]


class Backoff:
    """The subscriber's reconnect backoff, driven by the test instead of the clock.

    Each wait the subscriber asks for is recorded and held until the test
    releases it, so a test sees every delay, can change the world between
    attempts, and can stop the broker part-way through a wait.
    """

    def __init__(self) -> None:
        self.asked: list[float] = []
        self._requested = asyncio.Event()
        self._released = asyncio.Event()

    async def pause(self, seconds: float) -> None:
        self.asked.append(seconds)
        self._released.clear()
        self._requested.set()
        await self._released.wait()

    async def next(self) -> float:
        """The next delay asked for, once the subscriber is waiting it out."""
        await asyncio.wait_for(self._requested.wait(), FAIL_AFTER)
        self._requested.clear()
        return self.asked[-1]

    def release(self) -> None:
        self._released.set()


@pytest.fixture
def redis_url(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")


@pytest_asyncio.fixture
async def broker(redis_server, redis_url):
    """A broker wired to the shared Redis, with a manager of its own."""
    instance = make_broker(redis_server, "instance-a")
    try:
        yield instance
    finally:
        await shut(instance)


@pytest.fixture
def reconnecting(redis_server, redis_url, monkeypatch):
    """Let a broker that loses Redis find it again, quickly.

    A reconnecting broker builds its own client from REDIS_URL; here that is
    fakeredis on the shared server, as make_broker's first client is. The
    loss itself is fakeredis's own emulation of a server going away.
    """
    monkeypatch.setattr("redis.asyncio.from_url", lambda *args, **kwargs: client_for(redis_server))
    monkeypatch.setattr(broker_module, "RECONNECT_MIN_SECONDS", 0.01)
    monkeypatch.setattr(broker_module, "RECONNECT_MAX_SECONDS", 0.05)
    monkeypatch.setattr(broker_module, "UNAVAILABLE_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(broker_module, "SUBSCRIBER_POLL_SECONDS", 0.01)
    return redis_server


@pytest.mark.asyncio
class TestSequencing:
    async def test_sequences_increase(self, broker):
        first = await broker.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
        second = await broker.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
        assert (first.seq, second.seq) == (1, 2)

    async def test_each_workspace_counts_separately(self, broker):
        await broker.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
        await broker.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
        other = await broker.publish(make_event(EventType.CHAT_CREATED, "ws-2"))
        assert other.seq == 1, "one workspace's traffic must not advance another's"

    async def test_sequence_and_epoch_are_shared_across_instances(self, redis_server, redis_url):
        """Two API workers must not hand out the same number, or disagree on its epoch."""
        first, second = make_broker(redis_server, "a"), make_broker(redis_server, "b")
        try:
            a = await first.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1"))
            b = await second.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1"))
            assert (a.seq, b.seq) == (1, 2)
            assert a.epoch and a.epoch == b.epoch
        finally:
            await shut(first, second)

    async def test_an_event_without_a_workspace_is_refused(self, broker):
        result = await broker.publish(make_event(EventType.CHAT_CREATED, ""))
        assert result.seq == 0
        assert broker.stats["published"] == 0

    async def test_the_log_expires_but_the_counter_does_not(self, broker):
        """A counter that lapsed under an open tab would restart at 1 and be dropped."""
        await broker.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
        assert await broker._redis.ttl(log_key("ws-1")) > 0
        assert await broker._redis.ttl(sequence_key("ws-1")) == -1
        assert await broker._redis.ttl(epoch_key("ws-1")) == -1

    async def test_a_counter_restart_is_visible_as_a_new_epoch(self, broker):
        before = await broker.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
        await broker._redis.flushall()  # Redis losing its data
        after = await broker.publish(make_event(EventType.CHAT_CREATED, "ws-1"))

        assert after.seq == 1
        assert (
            after.epoch != before.epoch
        ), "without a new epoch, a client holding seq 1 would drop this as a duplicate"

    async def test_the_position_is_known_before_anything_is_published(self, broker):
        seq, epoch = await broker.current_position("fresh")
        assert seq == 0 and epoch
        first = await broker.publish(make_event(EventType.CHAT_CREATED, "fresh"))
        # A client that connected before the first event resumes in the
        # same sequence space it was told about.
        assert first.epoch == epoch
        assert await broker.current_position("fresh") == (1, epoch)

    async def test_without_redis_the_epoch_is_the_process(self, monkeypatch):
        monkeypatch.setattr(settings, "REDIS_URL", "")
        solo = EventBroker(manager=ConnectionManager(), instance_id="this-process")
        restarted = EventBroker(manager=ConnectionManager(), instance_id="after-reload")
        try:
            event = await solo.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
            assert event.epoch == "this-process"
            again = await restarted.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
            # A dev-server reload restarts the counter; the epoch says so.
            assert again.seq == 1 and again.epoch != event.epoch
        finally:
            await shut(solo, restarted)


@pytest.mark.asyncio
class TestLocalDelivery:
    async def test_publish_reaches_this_process_immediately(self, broker):
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)

        await broker.publish(make_event(EventType.DOCUMENT_STATUS, "ws-1", {"id": "d1"}))

        frames = await connection.websocket.wait_frames(1)
        assert frames[0]["seq"] == 1
        assert frames[0]["epoch"]

    async def test_delivery_does_not_depend_on_redis(self, broker, monkeypatch):
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)
        broker._redis = None
        monkeypatch.setattr(settings, "REDIS_URL", "")

        await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
        assert len(await connection.websocket.wait_frames(1)) == 1

    async def test_another_workspace_is_untouched(self, broker):
        mine = connect(broker.manager, "u1", "ws-1")
        theirs = connect(broker.manager, "u2", "ws-2")
        await broker.manager.register(mine)
        await broker.manager.register(theirs)

        await broker.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1"))
        assert theirs.queue.empty()

    async def test_the_manager_is_resolved_when_used(self, monkeypatch):
        """Replacing the process manager must not strand the broker on the old one."""
        monkeypatch.setattr(settings, "REDIS_URL", "")
        monkeypatch.setattr(manager_module, "_manager", None)
        broker = EventBroker()
        replacement = ConnectionManager()
        monkeypatch.setattr(manager_module, "_manager", replacement)
        try:
            assert broker.manager is replacement
        finally:
            await broker.stop()
            await replacement.stop()


@pytest.mark.asyncio
class TestCrossInstance:
    async def test_an_event_on_one_instance_reaches_a_client_on_another(
        self, redis_server, redis_url
    ):
        """The whole reason this layer exists."""
        publisher, listener = make_broker(redis_server, "api-1"), make_broker(redis_server, "api-2")
        connection = connect(listener.manager, "u1", "ws-1")
        await listener.manager.register(connection)
        try:
            await listening(listener)
            await publisher.publish(
                make_event(EventType.DOCUMENT_STATUS, "ws-1", {"id": "d1", "status": "indexed"})
            )

            frame = (await connection.websocket.wait_frames(1))[0]
            assert frame["type"] == EventType.DOCUMENT_STATUS
            assert frame["data"]["status"] == "indexed"
        finally:
            await shut(listener, publisher)

    async def test_an_instance_ignores_its_own_echo(self, redis_server, redis_url):
        """Publishing locally and over Redis must not deliver twice."""
        instance, other = make_broker(redis_server, "api-1"), make_broker(redis_server, "api-2")
        connection = connect(instance.manager, "u1", "ws-1")
        await instance.manager.register(connection)
        try:
            await listening(instance)
            await instance.publish(make_event(EventType.CHAT_MESSAGE, "ws-1", {"n": "mine"}))
            # Published after the echo on the same channel, so handled after
            # it: its arrival proves the echo has already been dealt with.
            await other.publish(make_event(EventType.CHAT_MESSAGE, "ws-1", {"n": "sentinel"}))

            frames = await connection.websocket.wait_frames(2)
            assert [f["data"]["n"] for f in frames] == [
                "mine",
                "sentinel",
            ], "the event was delivered twice"
            assert instance.stats["ignored_own"] == 1
        finally:
            await shut(instance, other)

    async def test_isolation_holds_across_instances(self, redis_server, redis_url):
        publisher, listener = make_broker(redis_server, "api-1"), make_broker(redis_server, "api-2")
        outsider = connect(listener.manager, "u2", "ws-other")
        await listener.manager.register(outsider)
        try:
            await listening(listener)
            await publisher.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "x"}))
            await publisher.publish(
                make_event(EventType.DOCUMENT_CREATED, "ws-other", {"id": "sentinel"})
            )

            frames = await outsider.websocket.wait_frames(1)
            assert [f["data"]["id"] for f in frames] == [
                "sentinel"
            ], "an event crossed workspaces over pub/sub"
        finally:
            await shut(listener, publisher)

    async def test_audience_rules_survive_the_trip(self, redis_server, redis_url):
        """A restricted event is still restricted after crossing Redis."""
        publisher, listener = make_broker(redis_server, "api-1"), make_broker(redis_server, "api-2")
        owner = connect(listener.manager, "owner", "ws-1")
        colleague = connect(listener.manager, "colleague", "ws-1")
        for connection in (owner, colleague):
            await listener.manager.register(connection)
        try:
            await listening(listener)
            await publisher.publish(
                make_event(EventType.CHAT_RENAMED, "ws-1", {"t": "private"}, recipient_id="owner")
            )
            await publisher.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"t": "all"}))

            assert [f["data"]["t"] for f in await colleague.websocket.wait_frames(1)] == ["all"]
            assert [f["data"]["t"] for f in await owner.websocket.wait_frames(2)] == [
                "private",
                "all",
            ]
        finally:
            await shut(listener, publisher)

    async def test_a_malformed_message_is_discarded(self, broker):
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)

        await broker._handle("not json at all")
        await broker._handle(json.dumps(["a", "list"]))

        assert connection.queue.empty()
        assert connection.websocket.frames == []

    async def test_a_message_from_the_worker_is_delivered(self, broker):
        """The worker sends origin=None because it serves no sockets itself."""
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)

        payload = make_event(EventType.DOCUMENT_STATUS, "ws-1", {"progress": 70}).with_sequence(4)
        wire = payload.to_wire()
        wire["origin"] = None
        await broker._handle(json.dumps(wire))

        assert (await connection.websocket.wait_frames(1))[0]["data"]["progress"] == 70


@pytest.mark.asyncio
class TestReplay:
    async def test_replay_returns_only_what_was_missed(self, broker):
        for _ in range(5):
            await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))

        missed = await broker.replay("ws-1", after_seq=3)
        assert [e.seq for e in missed] == [4, 5]

    async def test_replay_is_ordered(self, broker):
        for _ in range(6):
            await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
        missed = await broker.replay("ws-1", after_seq=0)
        assert [e.seq for e in missed] == [1, 2, 3, 4, 5, 6]

    async def test_replay_never_crosses_workspaces(self, broker):
        await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1", {"secret": True}))
        assert await broker.replay("ws-2", after_seq=0) == []

    async def test_replay_never_crosses_epochs(self, broker):
        await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
        assert await broker.replay("ws-1", after_seq=0, epoch="some-older-epoch") == []

    async def test_a_client_that_is_up_to_date_gets_nothing(self, broker):
        latest = await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
        assert await broker.replay("ws-1", after_seq=latest.seq) == []

    async def test_the_log_is_bounded_and_says_where_it_starts(self, broker, monkeypatch):
        monkeypatch.setattr(settings, "WS_REPLAY_BUFFER_SIZE", 5)
        for _ in range(20):
            await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))

        stored = await broker._redis.lrange(log_key("ws-1"), 0, -1)
        assert len(stored) == 5, "an unbounded log is an unbounded memory leak"

        # A client asking from 0 cannot be given 1..15; oldest_retained is
        # how the endpoint knows to say so rather than pretend.
        assert await broker.oldest_retained("ws-1") == 16
        assert [e.seq for e in await broker.replay("ws-1", after_seq=0)] == [16, 17, 18, 19, 20]

    async def test_replay_works_without_redis(self, monkeypatch):
        monkeypatch.setattr(settings, "REDIS_URL", "")
        instance = EventBroker(manager=ConnectionManager(), instance_id="solo")
        try:
            for _ in range(3):
                await instance.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
            missed = await instance.replay("ws-1", after_seq=1)
            assert [e.seq for e in missed] == [2, 3]
            assert await instance.oldest_retained("ws-1") == 1
            assert await instance.oldest_retained("empty") == 0
        finally:
            await shut(instance)


@pytest.mark.asyncio
class TestPresenceAcrossInstances:
    """Two API processes, one Redis: presence must be one picture."""

    async def test_tabs_on_different_instances_are_one_user(self, redis_server, redis_url):
        first, second = make_broker(redis_server, "api-1"), make_broker(redis_server, "api-2")
        try:
            assert await first.presence.arrive("ws-1", "u1", "tab-a") is True
            assert (
                await second.presence.arrive("ws-1", "u1", "tab-b") is False
            ), "a second tab is not a second arrival"
            assert await first.presence.online("ws-1") == ["u1"]
            assert await second.presence.online("ws-1") == ["u1"]

            assert (
                await first.presence.depart("ws-1", "u1", "tab-a") is False
            ), "closing one of two tabs must not mark the user offline"
            assert await second.presence.online("ws-1") == ["u1"]

            assert await second.presence.depart("ws-1", "u1", "tab-b") is True
            assert await first.presence.online("ws-1") == []
            assert await first.presence.last_seen("ws-1", "u1")
        finally:
            await shut(first, second)

    async def test_presence_is_scoped_to_the_workspace(self, broker):
        await broker.presence.arrive("ws-1", "u1", "tab-a")
        assert await broker.presence.online("ws-2") == []

    async def test_two_users_are_listed_once_each(self, broker):
        await broker.presence.arrive("ws-1", "u1", "tab-a")
        await broker.presence.arrive("ws-1", "u2", "tab-b")
        await broker.presence.arrive("ws-1", "u1", "tab-c")
        assert await broker.presence.online("ws-1") == ["u1", "u2"]

    async def test_presence_expires_if_its_process_dies(self, broker):
        """A crashed instance cannot run teardown; the TTL is what covers it."""
        await broker.presence.arrive("ws-1", "u1", "tab-a")
        assert await broker._redis.ttl(presence_key("ws-1", "u1")) > 0

    async def test_last_seen_outlives_presence(self, broker):
        await broker.presence.arrive("ws-1", "u1", "tab-a")
        await broker.presence.depart("ws-1", "u1", "tab-a")
        assert await broker._redis.exists(presence_key("ws-1", "u1")) == 0
        assert await broker._redis.ttl(last_seen_key("ws-1", "u1")) > 0


@pytest.mark.asyncio
class TestDegradation:
    async def test_publishing_survives_redis_going_away(self, broker, monkeypatch):
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)

        class BrokenRedis:
            def pipeline(self, *a, **k):
                raise ConnectionError("redis is gone")

            async def aclose(self):
                return None

        broker._redis = BrokenRedis()
        monkeypatch.setattr(settings, "REDIS_URL", "")  # no reconnect attempt
        event = await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))

        # The local client still gets it, in the local sequence space; only
        # the fan-out is lost.
        assert (event.seq, event.epoch) == (1, broker.instance_id)
        assert len(await connection.websocket.wait_frames(1)) == 1
        assert broker.stats["redis_errors"] >= 1

    async def test_start_without_redis_is_quiet(self, monkeypatch):
        monkeypatch.setattr(settings, "REDIS_URL", "")
        instance = EventBroker(manager=ConnectionManager())
        try:
            await instance.start()
            assert instance.snapshot()["listening"] is False
        finally:
            await shut(instance)

    async def test_the_subscription_signal_clears_on_stop(self, redis_server, redis_url):
        instance = make_broker(redis_server, "api-1")
        await listening(instance)
        assert instance.snapshot()["listening"] is True
        await shut(instance)
        assert instance.subscribed.is_set() is False


@pytest.mark.asyncio
class TestSubscription:
    """The subscriber's own life: staying up while quiet, and coming back."""

    async def test_a_quiet_subscription_is_pinged_not_dropped(
        self, redis_server, redis_url, monkeypatch
    ):
        """Regression: silence used to end the subscription.

        ``pubsub.listen()`` read under the client's command timeout, so against
        a real Redis every five quiet seconds tore the subscription down, and
        whatever other instances published while it came back was lost.
        fakeredis applies no read timeout, so that half is proved against a
        real server in test_realtime_redis.py. This half: silence leads to a
        ping, and its answer keeps the subscription -- a second ping is sent
        only once the first has been answered, so three pings on one
        subscription mean two answers were recognised.
        """
        monkeypatch.setattr(broker_module, "SUBSCRIBER_POLL_SECONDS", 0.01)
        monkeypatch.setattr(broker_module, "SUBSCRIBER_PING_SECONDS", 0.02)
        listener, publisher = make_broker(redis_server, "api-2"), make_broker(redis_server, "api-1")
        connection = connect(listener.manager, "u1", "ws-1")
        await listener.manager.register(connection)
        try:
            await listening(listener)
            await until(listener, lambda: listener.stats["pings"] >= 3)
            await publisher.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "after"}))

            assert (await connection.websocket.wait_frames(1))[0]["data"]["id"] == "after"
            assert listener.stats["redis_errors"] == 0
            assert listener.stats["subscriptions"] == 1, "the subscription was replaced"
        finally:
            await shut(listener, publisher)

    async def test_a_lost_subscription_comes_back_and_says_where_things_stand(self, reconnecting):
        server = reconnecting
        listener, publisher = make_broker(server, "api-2"), make_broker(server, "api-1")
        connection = connect(listener.manager, "u1", "ws-1")
        await listener.manager.register(connection)
        try:
            await listening(listener)
            seen = await publisher.publish(
                make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "seen"})
            )
            await connection.websocket.wait_frames(1)

            server.connected = False
            # Stays down until the server is back, so this cannot be missed.
            await until(listener, lambda: not listener.subscribed.is_set())
            server.connected = True

            ready = (await connection.websocket.wait_frames(2))[1]
            assert ready["type"] == EventType.CONNECTED
            assert ready["data"]["connectionId"] == connection.id
            assert (ready["data"]["seq"], ready["data"]["epoch"]) == (seen.seq, seen.epoch)
            assert listener.stats["subscriptions"] == 2
            assert listener.stats["reannounced"] == 1

            # The new subscription replaced the old one rather than joining
            # it, so what is published now arrives once. (That the server
            # holds exactly one is asserted against a real Redis in
            # test_realtime_redis.py: fakeredis's async connection never
            # closes its socket, so the old subscription stays registered.)
            for marker in ("once", "sentinel"):
                await publisher.publish(
                    make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": marker})
                )
            frames = await connection.websocket.wait_frames(4)
            assert [f["data"]["id"] for f in frames[2:]] == ["once", "sentinel"]
        finally:
            await shut(listener, publisher)

    async def test_what_was_missed_is_in_the_log_the_new_position_points_to(self, reconnecting):
        """The gap a lost subscription leaves is recoverable, and the client is told where it ends."""
        server = reconnecting
        listener, publisher = make_broker(server, "api-2"), make_broker(server, "api-1")
        connection = connect(listener.manager, "u1", "ws-1")
        await listener.manager.register(connection)
        try:
            await listening(listener)
            seen = await publisher.publish(
                make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "seen"})
            )
            await connection.websocket.wait_frames(1)

            # Held off Redis before losing it, so the listener cannot come
            # back until the other instance has published: the event is
            # certain to be missed, not merely likely to be.
            listener._unavailable_until = math.inf
            server.connected = False
            await until(listener, lambda: not listener.subscribed.is_set())
            server.connected = True
            missed = await publisher.publish(
                make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "missed"})
            )
            listener._unavailable_until = 0.0

            ready = (await connection.websocket.wait_frames(2))[1]
            assert ready["type"] == EventType.CONNECTED, "the missed event arrived live after all"
            assert ready["data"]["seq"] == missed.seq
            replayed = await listener.replay(
                "ws-1", after_seq=seen.seq, epoch=ready["data"]["epoch"]
            )
            assert [e.data["id"] for e in replayed] == ["missed"]
        finally:
            await shut(listener, publisher)

    async def test_redis_arriving_after_startup_moves_sockets_to_the_shared_sequence(
        self, reconnecting
    ):
        """Sockets served process-locally while Redis was away are told when it returns."""
        server = reconnecting
        server.connected = False
        listener = EventBroker(manager=ConnectionManager(), instance_id="api-2")
        connection = connect(listener.manager, "u1", "ws-1")
        await listener.manager.register(connection)
        try:
            await listener.start()
            # Nothing else has touched Redis yet: this is the subscriber failing.
            await until(listener, lambda: listener.stats["redis_errors"] >= 1)
            local = await listener.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1"))
            assert local.epoch == "api-2"

            server.connected = True
            ready = (await connection.websocket.wait_frames(2))[1]
            assert ready["type"] == EventType.CONNECTED
            assert ready["data"]["epoch"] not in (
                "",
                "api-2",
            ), "the socket was left in the process-local sequence"
        finally:
            await shut(listener)

    async def test_a_first_subscription_announces_nothing(self, redis_server, redis_url):
        """Nothing was missed before there was anything to miss."""
        listener = make_broker(redis_server, "api-2")
        connection = connect(listener.manager, "u1", "ws-1")
        await listener.manager.register(connection)
        try:
            await listening(listener)
            await make_broker(redis_server, "api-1").publish(
                make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "first-frame"})
            )
            frames = await connection.websocket.wait_frames(1)
            assert frames[0]["data"]["id"] == "first-frame"
            assert listener.stats["reannounced"] == 0
        finally:
            await shut(listener)

    async def test_retries_back_off_to_a_ceiling_and_start_over_once_subscribed(
        self, reconnecting, monkeypatch
    ):
        """Bounded both ways: every failed attempt waits, and no wait grows without limit."""
        server = reconnecting
        monkeypatch.setattr(broker_module, "RECONNECT_MIN_SECONDS", 1)
        monkeypatch.setattr(broker_module, "RECONNECT_MAX_SECONDS", 8)
        attempts = 0

        def connection_attempt(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            return client_for(server)

        monkeypatch.setattr("redis.asyncio.from_url", connection_attempt)
        server.connected = False
        listener = EventBroker(manager=ConnectionManager(), instance_id="api-2")
        backoff = Backoff()
        listener._pause = backoff.pause  # type: ignore[method-assign]
        try:
            await listener.start()
            asked: list[float] = []
            for _ in range(6):
                asked.append(await backoff.next())
                assert attempts == len(asked), "an attempt was made without waiting first"
                if len(asked) == 6:
                    server.connected = True
                backoff.release()
            assert asked == [1, 2, 4, 8, 8, 8]

            await until(listener, listener.subscribed.is_set)
            server.connected = False
            assert await backoff.next() == 1, "the backoff did not start over after a success"
        finally:
            await shut(listener)

    async def test_stopping_while_waiting_to_reconnect_ends_it_there(self, reconnecting):
        server = reconnecting
        server.connected = False
        listener = EventBroker(manager=ConnectionManager(), instance_id="api-2")
        backoff = Backoff()
        listener._pause = backoff.pause  # type: ignore[method-assign]
        observer = client_for(server)
        try:
            await listener.start()
            task = listener._subscriber
            await backoff.next()
            # An attempt made now would succeed; stopping must mean none is.
            server.connected = True

            await asyncio.wait_for(listener.stop(), FAIL_AFTER)
            assert task is not None and task.done()
            assert listener._subscriber is None
            assert running_subscribers() == []
            assert listener.stats["subscriptions"] == 0
            assert await subscribers(observer) == 0
        finally:
            await shut(listener)
            await observer.aclose()

    async def test_stopping_an_idle_subscriber_leaves_nothing_behind(self, redis_server, redis_url):
        """Stopped mid-read, not left waiting on a channel that may stay silent for hours.

        That the server releases the subscription as well is asserted against
        a real Redis in test_realtime_redis.py; fakeredis keeps it registered
        after the connection holding it has closed.
        """
        listener = make_broker(redis_server, "api-2")
        observer = client_for(redis_server)
        try:
            await listening(listener)
            assert await subscribers(observer) == 1
            task = listener._subscriber

            await asyncio.wait_for(listener.stop(), FAIL_AFTER)
            assert task is not None and task.done()
            assert listener._subscriber is None
            assert running_subscribers() == []
            assert listener.subscribed.is_set() is False
            assert listener._redis is None
        finally:
            await shut(listener)
            await observer.aclose()

    async def test_a_failing_redis_is_left_alone_for_the_backoff_window(
        self, reconnecting, monkeypatch
    ):
        """Regression: after one call failed, the next call tried Redis again.

        Presence swallowed its errors without telling the broker, and a failed
        command dropped the client without opening the window a failed connect
        opens. Against a Redis that has stopped answering, each call a
        handshake makes paid the full command timeout in turn. One failure now
        opens the window, and what follows goes straight to this process.
        """
        monkeypatch.setattr(broker_module, "UNAVAILABLE_BACKOFF_SECONDS", 60.0)
        server = reconnecting
        instance = make_broker(server, "api-1")
        connection = connect(instance.manager, "u1", "ws-1")
        await instance.manager.register(connection)
        try:
            server.connected = False
            assert await instance.presence.arrive("ws-1", "u1", connection.id) is True
            assert await instance.presence.online("ws-1") == ["u1"]
            await instance.presence.refresh("ws-1", "u1")
            event = await instance.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1"))
            assert await instance.current_position("ws-1") == (event.seq, "api-1")

            assert instance.stats["redis_errors"] == 1, "a call after the failure tried Redis again"
            assert event.epoch == "api-1"
        finally:
            await shut(instance)


@pytest.mark.asyncio
class TestRevocation:
    async def test_a_revoked_token_closes_its_sockets_on_every_instance(
        self, redis_server, redis_url
    ):
        here, there = make_broker(redis_server, "api-1"), make_broker(redis_server, "api-2")

        def opened_with(token_id: str, name: str) -> Connection:
            return Connection(
                id=name,
                websocket=SocketSink(),
                user_id="u1",
                workspace_id="ws-1",
                token_id=token_id,
            )

        revoked_here = opened_with("jti-1", "here")
        revoked_there = opened_with("jti-1", "there")
        other_token = opened_with("jti-2", "other")
        await here.manager.register(revoked_here)
        await there.manager.register(revoked_there)
        await there.manager.register(other_token)
        try:
            await listening(there)
            assert await here.revoke_token("jti-1") == 1

            await asyncio.wait_for(revoked_there.websocket.closed.wait(), FAIL_AFTER)
            for connection in (revoked_here, revoked_there):
                assert connection.websocket.closed_with == (CLOSE_UNAUTHORIZED, "token revoked")

            # Handled after the revocation on the same channel, so if the
            # other token's socket were going to be closed, it would be now.
            await here.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "sentinel"}))
            assert (await other_token.websocket.wait_frames(1))[0]["data"]["id"] == "sentinel"
            assert other_token.websocket.closed_with is None
        finally:
            await shut(here, there)

    async def test_an_unknown_instruction_is_ignored(self, broker):
        connection = Connection(
            id="c", websocket=SocketSink(), user_id="u1", workspace_id="ws-1", token_id="jti-1"
        )
        await broker.manager.register(connection)
        await broker._handle(json.dumps({"control": "close-everything", "origin": "elsewhere"}))
        await broker._handle(json.dumps({"control": "revoke_token", "origin": "elsewhere"}))
        assert connection.websocket.closed_with is None
        assert connection.websocket.frames == []


@pytest.mark.asyncio
async def test_an_unreachable_redis_is_not_retried_on_every_call(monkeypatch):
    """One failed attempt opens a window; calls inside it do not try again.

    A real, unreachable address rather than a stub: what is counted is
    connection attempts that actually happened.
    """
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1")
    instance = EventBroker(manager=ConnectionManager(), instance_id="local-only")
    try:
        assert await instance._client() is None
        assert await instance._client() is None
        event = await instance.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
        await instance.presence.arrive("ws-1", "u1", "tab")
        await instance.current_position("ws-1")

        assert instance.stats["redis_errors"] == 1, "every call paid for a connection attempt"
        assert event.epoch == "local-only"
    finally:
        await shut(instance)


class TestWorkerPublishing:
    """The Celery worker is a separate, synchronous process."""

    def test_publish_reports_failure_without_redis(self, monkeypatch):
        from app.core import redis_client

        monkeypatch.setattr(settings, "REDIS_URL", "")
        redis_client.reset()
        assert publish_from_worker(make_event(EventType.DOCUMENT_STATUS, "ws-1")) is False

    def test_publish_refuses_an_event_with_no_workspace(self):
        assert publish_from_worker(make_event(EventType.DOCUMENT_STATUS, "")) is False

    def test_publish_writes_to_redis_in_the_shared_sequence(self, redis_server, monkeypatch):
        from app.core import redis_client

        monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")
        sync_client = fakeredis.FakeRedis(server=redis_server, decode_responses=True)
        redis_client.set_client(sync_client)
        try:
            assert publish_from_worker(
                make_event(EventType.DOCUMENT_STATUS, "ws-1", {"progress": 35})
            )
            stored = json.loads(sync_client.lrange(log_key("ws-1"), 0, -1)[0])
            assert stored["data"]["progress"] == 35
            assert stored["seq"] == 1
            assert stored["epoch"] == sync_client.get(epoch_key("ws-1"))
        finally:
            redis_client.reset()

    def test_a_dropped_notification_never_raises(self, monkeypatch):
        """Ingestion must not fail because a notification could not be sent."""
        from app.core import redis_client

        monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")

        class BrokenRedis:
            def pipeline(self, *a, **k):
                raise ConnectionError("redis is gone")

        redis_client.set_client(BrokenRedis())
        try:
            assert publish_from_worker(make_event(EventType.DOCUMENT_STATUS, "ws-1")) is False
        finally:
            redis_client.reset()


@pytest.mark.asyncio
async def test_worker_event_reaches_a_browser(redis_server, redis_url):
    """End to end across processes: Celery publishes, an API instance delivers.

    The path a user actually sees -- indexing progress moving in the browser
    while a completely separate process does the work -- and in the same
    sequence space as events the API publishes itself.
    """
    from app.core import redis_client

    api = make_broker(redis_server, "api-1")
    connection = connect(api.manager, "u1", "ws-1")
    await api.manager.register(connection)
    redis_client.set_client(fakeredis.FakeRedis(server=redis_server, decode_responses=True))
    try:
        await listening(api)
        own = await api.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "d1"}))

        # The worker is synchronous and has no event loop of its own.
        delivered = await asyncio.get_running_loop().run_in_executor(
            None,
            publish_from_worker,
            make_event(EventType.DOCUMENT_STATUS, "ws-1", {"progress": 70, "status": "processing"}),
        )
        assert delivered is True

        frames = await connection.websocket.wait_frames(2)
        assert frames[1]["data"]["progress"] == 70
        assert (frames[1]["seq"], frames[1]["epoch"]) == (own.seq + 1, own.epoch)
    finally:
        redis_client.reset()
        await shut(api)


def test_keys_are_namespaced():
    """A shared Redis must not have these collide with anything else."""
    for key in (CHANNEL, sequence_key("w"), log_key("w"), epoch_key("w")):
        assert key.startswith("researchsphere:")
