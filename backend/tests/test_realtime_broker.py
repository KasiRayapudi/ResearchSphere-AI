"""
Cross-instance event delivery.

Redis here is fakeredis, which implements the protocol in process rather than
stubbing the calls out: pub/sub really does carry messages between two
clients of the same server, INCR really is shared, and LTRIM really discards
the oldest entries. That is what makes a two-broker test meaningful -- the
fan-out logic is exercised, not asserted against a recording of itself.

What it does not cover is a real Redis server over a real socket: no Redis
is reachable in this environment. Reconnection against a genuinely restarting
server is therefore unproven here, and is called out as such.
"""

import asyncio
import json

import fakeredis
import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.core.config import settings
from app.realtime.broker import CHANNEL, EventBroker, log_key, publish_from_worker, sequence_key
from app.realtime.events import EventType, make_event
from app.realtime.manager import Connection, ConnectionManager

from .test_realtime_manager import SocketSink, settle


def connect(manager: ConnectionManager, user: str, workspace: str) -> Connection:
    sink = SocketSink()
    return Connection(id=f"{user}-{id(sink)}", websocket=sink, user_id=user, workspace_id=workspace)


@pytest.fixture
def redis_server():
    """One Redis, shared by every client in a test, as in a deployment."""
    return fakeredis.FakeServer()


def client_for(server) -> "fakeredis.aioredis.FakeRedis":
    return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)


@pytest_asyncio.fixture
async def broker(redis_server, monkeypatch):
    """A broker wired to the shared Redis, with a manager of its own."""
    monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")
    manager = ConnectionManager()
    instance = EventBroker(manager=manager, instance_id="instance-a")
    instance._redis = client_for(redis_server)
    try:
        yield instance
    finally:
        await instance.stop()
        await manager.stop()


async def wait_for(predicate, timeout: float = 3.0) -> bool:
    """Poll until something becomes true, for genuinely asynchronous delivery."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


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

    async def test_sequence_is_shared_across_instances(self, redis_server, monkeypatch):
        """Two API workers must not hand out the same sequence number."""
        monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")
        first = EventBroker(manager=ConnectionManager(), instance_id="a")
        second = EventBroker(manager=ConnectionManager(), instance_id="b")
        first._redis = client_for(redis_server)
        second._redis = client_for(redis_server)
        try:
            a = await first.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1"))
            b = await second.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1"))
            assert (a.seq, b.seq) == (1, 2)
        finally:
            await first.stop()
            await second.stop()

    async def test_an_event_without_a_workspace_is_refused(self, broker):
        result = await broker.publish(make_event(EventType.CHAT_CREATED, ""))
        assert result.seq == 0
        assert broker.stats["published"] == 0

    async def test_state_keys_expire(self, broker):
        await broker.publish(make_event(EventType.CHAT_CREATED, "ws-1"))
        # A workspace nobody touches again should stop costing memory.
        assert await broker._redis.ttl(sequence_key("ws-1")) > 0
        assert await broker._redis.ttl(log_key("ws-1")) > 0


@pytest.mark.asyncio
class TestLocalDelivery:
    async def test_publish_reaches_this_process_immediately(self, broker):
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)

        await broker.publish(make_event(EventType.DOCUMENT_STATUS, "ws-1", {"id": "d1"}))
        await settle()

        assert len(connection.websocket.frames) == 1
        assert connection.websocket.frames[0]["seq"] == 1

    async def test_delivery_does_not_wait_for_redis(self, broker):
        """Local delivery must not depend on the round trip, or on Redis at all."""
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)
        broker._redis = None
        monkey = settings.REDIS_URL
        settings.REDIS_URL = ""
        try:
            await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
            await settle()
            assert len(connection.websocket.frames) == 1
        finally:
            settings.REDIS_URL = monkey

    async def test_another_workspace_is_untouched(self, broker):
        mine = connect(broker.manager, "u1", "ws-1")
        theirs = connect(broker.manager, "u2", "ws-2")
        await broker.manager.register(mine)
        await broker.manager.register(theirs)

        await broker.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1"))
        await settle()
        assert theirs.websocket.frames == []


@pytest.mark.asyncio
class TestCrossInstance:
    async def test_an_event_on_one_instance_reaches_a_client_on_another(
        self, redis_server, monkeypatch
    ):
        """The whole reason this layer exists."""
        monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")
        publisher = EventBroker(manager=ConnectionManager(), instance_id="api-1")
        listener = EventBroker(manager=ConnectionManager(), instance_id="api-2")
        publisher._redis = client_for(redis_server)
        listener._redis = client_for(redis_server)

        connection = connect(listener.manager, "u1", "ws-1")
        await listener.manager.register(connection)
        await listener.start()

        try:
            assert await wait_for(lambda: listener.stats["received"] >= 0)
            await asyncio.sleep(0.1)  # let the subscription land

            await publisher.publish(
                make_event(EventType.DOCUMENT_STATUS, "ws-1", {"id": "d1", "status": "indexed"})
            )

            assert await wait_for(
                lambda: len(connection.websocket.frames) == 1
            ), "an event published on one instance never reached the other"
            frame = connection.websocket.frames[0]
            assert frame["type"] == EventType.DOCUMENT_STATUS
            assert frame["data"]["status"] == "indexed"
        finally:
            await listener.stop()
            await publisher.stop()
            await listener.manager.stop()
            await publisher.manager.stop()

    async def test_an_instance_ignores_its_own_echo(self, redis_server, monkeypatch):
        """Publishing locally and over Redis must not deliver twice."""
        monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")
        instance = EventBroker(manager=ConnectionManager(), instance_id="api-1")
        instance._redis = client_for(redis_server)

        connection = connect(instance.manager, "u1", "ws-1")
        await instance.manager.register(connection)
        await instance.start()
        try:
            await asyncio.sleep(0.1)
            await instance.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
            await asyncio.sleep(0.3)

            assert len(connection.websocket.frames) == 1, "the event was delivered twice"
            assert instance.stats["ignored_own"] == 1
        finally:
            await instance.stop()
            await instance.manager.stop()

    async def test_isolation_holds_across_instances(self, redis_server, monkeypatch):
        monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")
        publisher = EventBroker(manager=ConnectionManager(), instance_id="api-1")
        listener = EventBroker(manager=ConnectionManager(), instance_id="api-2")
        publisher._redis = client_for(redis_server)
        listener._redis = client_for(redis_server)

        outsider = connect(listener.manager, "u2", "ws-other")
        await listener.manager.register(outsider)
        await listener.start()
        try:
            await asyncio.sleep(0.1)
            await publisher.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "x"}))
            await asyncio.sleep(0.3)
            assert outsider.websocket.frames == [], "an event crossed workspaces over pub/sub"
        finally:
            await listener.stop()
            await publisher.stop()
            await listener.manager.stop()
            await publisher.manager.stop()

    async def test_a_malformed_message_is_discarded(self, broker):
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)

        await broker._handle("not json at all")
        await broker._handle(json.dumps(["a", "list"]))
        await settle()

        assert connection.websocket.frames == []

    async def test_a_message_from_the_worker_is_delivered(self, broker):
        """The worker sends origin=None because it serves no sockets itself."""
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)

        payload = make_event(EventType.DOCUMENT_STATUS, "ws-1", {"progress": 70}).with_sequence(4)
        wire = payload.to_wire()
        wire["origin"] = None
        await broker._handle(json.dumps(wire))
        await settle()

        assert connection.websocket.frames[0]["data"]["progress"] == 70


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
        assert [e.seq for e in missed] == sorted(e.seq for e in missed)

    async def test_replay_never_crosses_workspaces(self, broker):
        await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1", {"secret": True}))
        assert await broker.replay("ws-2", after_seq=0) == []

    async def test_a_client_that_is_up_to_date_gets_nothing(self, broker):
        latest = await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
        assert await broker.replay("ws-1", after_seq=latest.seq) == []

    async def test_the_log_is_bounded(self, broker, monkeypatch):
        monkeypatch.setattr(settings, "WS_REPLAY_BUFFER_SIZE", 5)
        for _ in range(20):
            await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))

        stored = await broker._redis.lrange(log_key("ws-1"), 0, -1)
        assert len(stored) == 5, "an unbounded log is an unbounded memory leak"

        missed = await broker.replay("ws-1", after_seq=0)
        # The client asked from 0 but the oldest surviving event is 16: it can
        # see the gap and refetch rather than silently missing 15 events.
        assert missed[0].seq == 16

    async def test_replay_works_without_redis(self, monkeypatch):
        monkeypatch.setattr(settings, "REDIS_URL", "")
        manager = ConnectionManager()
        instance = EventBroker(manager=manager, instance_id="solo")
        try:
            for _ in range(3):
                await instance.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
            missed = await instance.replay("ws-1", after_seq=1)
            assert [e.seq for e in missed] == [2, 3]
        finally:
            await instance.stop()
            await manager.stop()


@pytest.mark.asyncio
class TestDegradation:
    async def test_publishing_survives_redis_going_away(self, broker):
        connection = connect(broker.manager, "u1", "ws-1")
        await broker.manager.register(connection)

        class BrokenRedis:
            async def incr(self, *a, **k):
                raise ConnectionError("redis is gone")

            async def aclose(self):
                return None

        broker._redis = BrokenRedis()
        settings_url = settings.REDIS_URL
        settings.REDIS_URL = ""  # no reconnect attempt during the test
        try:
            event = await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
            await settle()
            # The local client still gets it; only the fan-out is lost.
            assert event.seq == 1
            assert len(connection.websocket.frames) == 1
            assert broker.stats["redis_errors"] >= 1
        finally:
            settings.REDIS_URL = settings_url

    async def test_start_without_redis_is_quiet(self, monkeypatch):
        monkeypatch.setattr(settings, "REDIS_URL", "")
        manager = ConnectionManager()
        instance = EventBroker(manager=manager)
        try:
            await instance.start()
            assert instance.snapshot()["listening"] is False
        finally:
            await instance.stop()
            await manager.stop()


class TestWorkerPublishing:
    """The Celery worker is a separate, synchronous process."""

    def test_publish_reports_failure_without_redis(self, monkeypatch):
        from app.core import redis_client

        monkeypatch.setattr(settings, "REDIS_URL", "")
        redis_client.reset()
        assert publish_from_worker(make_event(EventType.DOCUMENT_STATUS, "ws-1")) is False

    def test_publish_refuses_an_event_with_no_workspace(self):
        assert publish_from_worker(make_event(EventType.DOCUMENT_STATUS, "")) is False

    def test_publish_writes_to_redis(self, redis_server, monkeypatch):
        from app.core import redis_client

        monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")
        sync_client = fakeredis.FakeRedis(server=redis_server, decode_responses=True)
        redis_client.set_client(sync_client)
        try:
            ok = publish_from_worker(
                make_event(EventType.DOCUMENT_STATUS, "ws-1", {"progress": 35})
            )
            assert ok is True

            stored = sync_client.lrange(log_key("ws-1"), 0, -1)
            assert len(stored) == 1
            assert json.loads(stored[0])["data"]["progress"] == 35
            assert int(sync_client.get(sequence_key("ws-1"))) == 1
        finally:
            redis_client.reset()

    def test_a_dropped_notification_never_raises(self, monkeypatch):
        """Ingestion must not fail because a notification could not be sent."""
        from app.core import redis_client

        monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")

        class BrokenRedis:
            def incr(self, *a, **k):
                raise ConnectionError("redis is gone")

        redis_client.set_client(BrokenRedis())
        try:
            assert publish_from_worker(make_event(EventType.DOCUMENT_STATUS, "ws-1")) is False
        finally:
            redis_client.reset()


@pytest.mark.asyncio
async def test_worker_event_reaches_a_browser(redis_server, monkeypatch):
    """End to end across processes: Celery publishes, an API instance delivers.

    This is the path a user actually sees -- indexing progress moving in the
    browser while a completely separate process does the work.
    """
    from app.core import redis_client

    monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")
    api = EventBroker(manager=ConnectionManager(), instance_id="api-1")
    api._redis = client_for(redis_server)

    connection = connect(api.manager, "u1", "ws-1")
    await api.manager.register(connection)
    await api.start()

    redis_client.set_client(fakeredis.FakeRedis(server=redis_server, decode_responses=True))
    try:
        await asyncio.sleep(0.1)
        # The worker is synchronous and has no event loop of its own.
        await asyncio.get_event_loop().run_in_executor(
            None,
            publish_from_worker,
            make_event(EventType.DOCUMENT_STATUS, "ws-1", {"progress": 70, "status": "processing"}),
        )

        assert await wait_for(
            lambda: len(connection.websocket.frames) == 1
        ), "the worker's progress update never reached the browser"
        assert connection.websocket.frames[0]["data"]["progress"] == 70
    finally:
        redis_client.reset()
        await api.stop()
        await api.manager.stop()


def test_channel_name_is_namespaced():
    """A shared Redis must not have this collide with anything else."""
    assert CHANNEL.startswith("researchsphere:")
    assert sequence_key("w").startswith("researchsphere:")
    assert log_key("w").startswith("researchsphere:")
