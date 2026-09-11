"""
Cross-instance event delivery, sequencing and presence.

Redis here is fakeredis, which implements the protocol in process rather than
stubbing the calls out: pub/sub really does carry messages between two
clients of the same server, INCR and SET NX really are shared, and LTRIM
really discards the oldest entries. That is what makes a two-broker test
meaningful -- the fan-out logic is exercised, not asserted against a
recording of itself.

What it does not cover is a real Redis server over a real socket: none is
reachable in this environment. Reconnection against a genuinely restarting
server is therefore unproven here, and is called out as such.

No test waits on the clock. Subscription is awaited through the broker's
``subscribed`` event; arrival through the sink's frame signal; and absence is
proved with a sentinel -- pub/sub delivers one channel in order to one
subscriber, so once a later message has been handled, an earlier one has
been too.
"""

import asyncio
import json

import fakeredis
import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.core.config import settings
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
from app.realtime.manager import Connection, ConnectionManager
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
