"""
The realtime layer against a real Redis server.

The rest of the realtime suite runs on fakeredis in process. That proves the
logic -- sequencing, fan-out, audience, replay, reconnection -- but not the
wire. Its connection applies no read timeout, never loses its socket, has no
server that can stop answering, and keeps a subscription registered after
the connection holding it is closed. Those are what break a pub/sub
subscriber in production: a subscription that tore itself down after five
quiet seconds passed the whole fakeredis suite for exactly that reason.

Against a real server over real sockets, this module covers startup and
shutdown as the server sees them; delivery between instances, from the
worker's blocking client, and of a revocation; a quiet subscription
outliving the command timeout, with the production settings as well as
shortened ones; a ping nobody answers; a connection the server kills;
recovery, with this instance's sockets told to resume and nothing delivered
twice; shutdown in the middle of an outage; MULTI/EXEC sequencing under
concurrent publishers; the capped replay log; presence found by SCAN among
unrelated keys; and the whole path through the application's endpoint.

It is opt-in. Point REALTIME_REDIS_URL at a Redis this suite may use
destructively -- it flushes its database and kills pub/sub clients across
the whole server -- so a throwaway one, never a shared one::

    docker compose up -d redis
    REALTIME_REDIS_URL=redis://localhost:6379/15 pytest tests/test_realtime_redis.py

CI runs it against a redis:7-alpine service container, the image
docker-compose.yml runs. Without the variable every test here is skipped and
says why; with it set and the server unreachable, they fail.

A connection that stays open while nothing crosses it -- a hung server, a
path that silently drops packets -- is made with a relay the test controls,
placed between the broker and the real server. Frozen, it holds every byte
in both directions. That is a fault injected on the network, not a stand-in
for Redis: every command is still answered by the real server.

Waiting: the broker's own state is awaited through its ``changed`` signal,
and sockets through their frame signal. The server's view of its clients is
the exception -- Redis notices a closed connection when it next reads that
socket, in its own time -- so ``server_reports`` asks it until it agrees,
bounded, and without sleeping between asks.
"""

import asyncio
import json
import os
import time
import uuid
from urllib.parse import urlparse

import pytest
import pytest_asyncio
import redis
import redis.asyncio as aioredis

from app.core import redis_client
from app.core.config import settings
from app.realtime import broker as broker_module
from app.realtime.broker import (
    STATE_TTL_SECONDS,
    EventBroker,
    epoch_key,
    get_broker,
    log_key,
    publish_from_worker,
    sequence_key,
)
from app.realtime.events import Event, EventType, make_event
from app.realtime.manager import CLOSE_UNAUTHORIZED, Connection, ConnectionManager
from app.realtime.presence import PRESENCE_TTL_SECONDS, presence_key
from tests import test_realtime_ws as ws
from tests.test_realtime_broker import listening, running_subscribers, shut, subscribers, until
from tests.test_realtime_manager import FAIL_AFTER, SocketSink

REAL_REDIS_URL = os.environ.get("REALTIME_REDIS_URL", "")

#: Bound on recovering a lost connection: the first backoff, a reconnect, and
#: slack for a busy CI runner. A failure bound, never a delay.
RECOVERY = 20.0

pytestmark = [
    pytest.mark.redis,
    pytest.mark.skipif(
        not REAL_REDIS_URL, reason="REALTIME_REDIS_URL is not set: no real Redis to test against"
    ),
]

owner = ws.owner


# ------------------------------------------------------------------ helpers --
class Relay:
    """A TCP relay between the broker and Redis that a test can freeze.

    Frozen, bytes read in either direction are held instead of passed on, so
    both ends see an open connection on which nothing arrives. Thawed, they
    flow again, late.
    """

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._flowing = asyncio.Event()
        self._flowing.set()
        self._writers: list[asyncio.StreamWriter] = []
        self._links: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def freeze(self) -> None:
        self._flowing.clear()

    def thaw(self) -> None:
        self._flowing.set()

    async def close(self) -> None:
        self.thaw()
        for writer in self._writers:
            writer.close()
        self._server.close()
        if self._links:
            await asyncio.wait(self._links, timeout=FAIL_AFTER)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        link = asyncio.current_task()
        if link is not None:
            self._links.add(link)
        try:
            up_reader, up_writer = await asyncio.open_connection(*self._target)
            self._writers += [writer, up_writer]
            await asyncio.gather(
                self._pump(reader, up_writer), self._pump(up_reader, writer), return_exceptions=True
            )
        finally:
            if link is not None:
                self._links.discard(link)

    async def _pump(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while data := await source.read(65536):
                await self._flowing.wait()
                sink.write(data)
                await sink.drain()
        finally:
            sink.close()


def instance(name: str) -> EventBroker:
    """An API instance: its own manager, and its own connections to the server."""
    return EventBroker(manager=ConnectionManager(), instance_id=name)


async def attached(broker: EventBroker, workspace: str = "ws-1", **fields) -> Connection:
    connection = Connection(
        id=uuid.uuid4().hex,
        websocket=SocketSink(),
        user_id=fields.pop("user_id", "u1"),
        workspace_id=workspace,
        **fields,
    )
    await broker.manager.register(connection)
    return connection


async def server_reports(server, expected: int, bound: float = FAIL_AFTER) -> None:
    """Ask the server how many subscribers it holds until it says ``expected``."""
    deadline = time.monotonic() + bound
    while (actual := await subscribers(server)) != expected:
        if time.monotonic() > deadline:
            raise AssertionError(f"the server holds {actual} subscriptions, not {expected}")


def shortened(monkeypatch, **overrides: float) -> None:
    """Scale the broker's timings down so a test takes a fraction of a second."""
    values = {
        "SUBSCRIBER_POLL_SECONDS": 0.05,
        "SUBSCRIBER_PING_SECONDS": 0.2,
        "SUBSCRIBER_REPLY_TIMEOUT_SECONDS": 0.5,
        "COMMAND_TIMEOUT_SECONDS": 0.5,
        "RECONNECT_MIN_SECONDS": 0.05,
        "RECONNECT_MAX_SECONDS": 0.2,
        "UNAVAILABLE_BACKOFF_SECONDS": 0.1,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(broker_module, name, value)


# ----------------------------------------------------------------- fixtures --
@pytest.fixture
def real_redis(monkeypatch):
    """The server under test, emptied, with the application pointed at it."""
    admin = redis.Redis.from_url(REAL_REDIS_URL, decode_responses=True, socket_timeout=10)
    try:
        admin.ping()
    except redis.RedisError as exc:
        pytest.fail(f"REALTIME_REDIS_URL is set, but {REAL_REDIS_URL} cannot be reached: {exc}")
    admin.flushdb()
    monkeypatch.setattr(settings, "REDIS_URL", REAL_REDIS_URL)
    redis_client.reset()
    try:
        yield admin
    finally:
        redis_client.reset()
        admin.flushdb()
        admin.close()


@pytest_asyncio.fixture
async def server(real_redis):
    """The same server, from the test's own event loop."""
    client = aioredis.from_url(REAL_REDIS_URL, decode_responses=True, socket_timeout=10)
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def relay(real_redis, monkeypatch):
    """A freezable relay to the server, with the application pointed through it."""
    target = urlparse(REAL_REDIS_URL)
    link = Relay(target.hostname or "localhost", target.port or 6379)
    await link.start()
    credentials = target.netloc.rpartition("@")[0]
    address = f"127.0.0.1:{link.port}"
    netloc = f"{credentials}@{address}" if credentials else address
    monkeypatch.setattr(settings, "REDIS_URL", target._replace(netloc=netloc).geturl())
    try:
        yield link
    finally:
        await link.close()


# ------------------------------------------------------------------ startup --
@pytest.mark.asyncio
class TestStartup:
    async def test_subscribed_means_the_server_holds_the_subscription(self, server):
        broker = instance("api-1")
        try:
            await listening(broker)
            # Set only once the server has confirmed: nothing to wait for.
            assert await subscribers(server) == 1
            assert broker.snapshot()["redis"] is True
        finally:
            await shut(broker)
        await server_reports(server, 0)


# ----------------------------------------------------------------- delivery --
@pytest.mark.asyncio
class TestDelivery:
    async def test_an_event_crosses_between_instances(self, server):
        publisher, listener = instance("api-1"), instance("api-2")
        mine = await attached(listener, "ws-1")
        theirs = await attached(listener, "ws-2", user_id="u2")
        try:
            await listening(listener)
            sent = await publisher.publish(
                make_event(EventType.DOCUMENT_STATUS, "ws-1", {"id": "d1", "status": "indexed"})
            )
            await publisher.publish(make_event(EventType.DOCUMENT_CREATED, "ws-2", {"id": "s"}))

            frame = (await mine.websocket.wait_frames(1))[0]
            assert (frame["type"], frame["data"]["status"]) == (
                EventType.DOCUMENT_STATUS,
                "indexed",
            )
            assert (frame["seq"], frame["epoch"]) == (sent.seq, sent.epoch)
            assert [f["data"]["id"] for f in await theirs.websocket.wait_frames(1)] == ["s"]
            assert len(mine.websocket.frames) == 1, "an event crossed workspaces"
        finally:
            await shut(listener, publisher)

    async def test_an_instance_does_not_deliver_its_own_echo(self, server):
        mine, other = instance("api-1"), instance("api-2")
        connection = await attached(mine)
        try:
            await listening(mine)
            await mine.publish(make_event(EventType.CHAT_MESSAGE, "ws-1", {"n": "own"}))
            await other.publish(make_event(EventType.CHAT_MESSAGE, "ws-1", {"n": "sentinel"}))
            frames = await connection.websocket.wait_frames(2)
            assert [f["data"]["n"] for f in frames] == ["own", "sentinel"]
            assert mine.stats["ignored_own"] == 1
        finally:
            await shut(mine, other)

    async def test_a_worker_event_reaches_an_instance(self, server):
        listener = instance("api-2")
        connection = await attached(listener)
        try:
            await listening(listener)
            delivered = await asyncio.to_thread(
                publish_from_worker,
                make_event(EventType.DOCUMENT_STATUS, "ws-1", {"progress": 70}),
            )
            assert delivered is True, "the worker's blocking client could not publish"
            frame = (await connection.websocket.wait_frames(1))[0]
            assert frame["data"]["progress"] == 70
            assert frame["epoch"] == (await listener.current_position("ws-1"))[1]
        finally:
            await shut(listener)

    async def test_a_revocation_reaches_another_instance(self, server):
        here, there = instance("api-1"), instance("api-2")
        revoked = await attached(there, token_id="jti-1")
        other = await attached(there, token_id="jti-2")
        try:
            await listening(there)
            assert await here.revoke_token("jti-1") == 0  # none of its sockets here
            await asyncio.wait_for(revoked.websocket.closed.wait(), FAIL_AFTER)
            assert revoked.websocket.closed_with == (CLOSE_UNAUTHORIZED, "token revoked")

            await here.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "sentinel"}))
            assert (await other.websocket.wait_frames(1))[0]["data"]["id"] == "sentinel"
            assert other.websocket.closed_with is None
        finally:
            await shut(here, there)


# ------------------------------------------------------- quiet subscription --
@pytest.mark.asyncio
class TestQuietSubscription:
    async def test_a_quiet_channel_outlives_the_command_timeout(self, server, monkeypatch):
        """Regression: silence used to end the subscription.

        ``pubsub.listen()`` read under the client's command timeout, so every
        quiet spell raised TimeoutError, the subscriber reconnected, and
        whatever another instance published meanwhile was lost. Timings are
        scaled down: each read waits longer than the command timeout, and the
        first ping comes only after several such reads.
        """
        shortened(
            monkeypatch,
            COMMAND_TIMEOUT_SECONDS=0.2,
            SUBSCRIBER_POLL_SECONDS=0.5,
            SUBSCRIBER_PING_SECONDS=1.5,
        )
        publisher, listener = instance("api-1"), instance("api-2")
        connection = await attached(listener)
        try:
            await listening(listener)
            await until(listener, lambda: listener.stats["pings"] >= 2, RECOVERY)
            assert listener.stats["redis_errors"] == 0, "a quiet read was treated as a failure"
            assert listener.stats["subscriptions"] == 1

            await publisher.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "after"}))
            assert (await connection.websocket.wait_frames(1))[0]["data"]["id"] == "after"
        finally:
            await shut(listener, publisher)

    async def test_with_production_settings_it_stays_up_until_its_first_ping(self, server):
        """Nothing scaled: fifteen silent seconds, three times the command timeout."""
        listener = instance("api-2")
        try:
            await listening(listener)
            await until(
                listener,
                lambda: listener.stats["pings"] >= 1,
                broker_module.SUBSCRIBER_PING_SECONDS + RECOVERY,
            )
            assert listener.stats["redis_errors"] == 0
            assert listener.stats["subscriptions"] == 1
            assert await subscribers(server) == 1
        finally:
            await shut(listener)


# --------------------------------------------------------- lost connections --
@pytest.mark.asyncio
class TestLostConnections:
    async def test_a_ping_nobody_answers_ends_the_connection_and_it_comes_back(
        self, relay, server, monkeypatch
    ):
        shortened(monkeypatch)
        publisher, listener = instance("api-1"), instance("api-2")
        connection = await attached(listener)
        try:
            await listening(listener)
            relay.freeze()
            await until(listener, lambda: not listener.subscribed.is_set(), RECOVERY)
            assert listener.stats["pings"] >= 1, "given up on without having asked"

            relay.thaw()
            ready = (await connection.websocket.wait_frames(1, timeout=RECOVERY))[0]
            assert ready["type"] == EventType.CONNECTED
            assert listener.stats["subscriptions"] == 2
            # The abandoned connection's subscription goes with it.
            await server_reports(server, 1)

            for marker in ("once", "sentinel"):
                await publisher.publish(
                    make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": marker})
                )
            frames = await connection.websocket.wait_frames(3)
            assert [f["data"]["id"] for f in frames[1:]] == ["once", "sentinel"]
        finally:
            await shut(listener, publisher)

    async def test_a_killed_connection_is_replaced_and_its_sockets_told_to_resume(self, server):
        publisher, listener = instance("api-1"), instance("api-2")
        connection = await attached(listener)
        try:
            await listening(listener)
            seen = await publisher.publish(
                make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "seen"})
            )
            await connection.websocket.wait_frames(1)

            # Only the listener subscribes, so only its connection goes -- and
            # the server removes it before answering, so the next publish has
            # no subscriber to reach.
            assert await server.execute_command("CLIENT", "KILL", "TYPE", "pubsub") == 1
            missed = await publisher.publish(
                make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": "missed"})
            )

            ready = (await connection.websocket.wait_frames(2, timeout=RECOVERY))[1]
            assert ready["type"] == EventType.CONNECTED, f"expected ready again, got {ready}"
            assert (ready["data"]["seq"], ready["data"]["epoch"]) == (missed.seq, missed.epoch)
            replayed = await listener.replay("ws-1", after_seq=seen.seq, epoch=missed.epoch)
            assert [e.data["id"] for e in replayed] == ["missed"]

            assert await subscribers(server) == 1
            for marker in ("once", "sentinel"):
                await publisher.publish(
                    make_event(EventType.DOCUMENT_CREATED, "ws-1", {"id": marker})
                )
            frames = await connection.websocket.wait_frames(4)
            assert [f["data"]["id"] for f in frames[2:]] == ["once", "sentinel"]
            assert listener.stats["subscriptions"] == 2
        finally:
            await shut(listener, publisher)

    async def test_stopping_during_an_outage_ends_cleanly(self, relay, server, monkeypatch):
        shortened(monkeypatch)
        listener = instance("api-2")
        try:
            await listening(listener)
            relay.freeze()
            await until(listener, lambda: not listener.subscribed.is_set(), RECOVERY)
            task = listener._subscriber

            await asyncio.wait_for(listener.stop(), FAIL_AFTER)
            assert task is not None and task.done()
            assert running_subscribers() == []
            relay.thaw()
            await server_reports(server, 0)
        finally:
            await shut(listener)

    async def test_a_hung_server_costs_one_timeout_per_window_not_one_per_call(
        self, relay, server, monkeypatch
    ):
        """What a handshake does while Redis has stopped answering."""
        shortened(monkeypatch, UNAVAILABLE_BACKOFF_SECONDS=60.0)
        broker = instance("api-1")
        connection = await attached(broker)
        try:
            assert await broker._client() is not None
            relay.freeze()
            started = time.monotonic()
            await broker.presence.arrive("ws-1", "u1", connection.id)
            await broker.presence.online("ws-1")
            await broker.current_position("ws-1")
            event = await broker.publish(make_event(EventType.DOCUMENT_CREATED, "ws-1"))
            elapsed = time.monotonic() - started

            assert broker.stats["redis_errors"] == 1
            assert event.epoch == "api-1"
            # One command timeout, not four; the bound is loose for a busy runner.
            assert elapsed < 4 * broker_module.COMMAND_TIMEOUT_SECONDS
        finally:
            relay.thaw()
            await shut(broker)


# ------------------------------------------------------ sequencing and log --
@pytest.mark.asyncio
class TestSequencing:
    async def test_concurrent_publishers_share_one_gapless_sequence(self, server):
        first, second = instance("api-1"), instance("api-2")
        try:
            # Connected before the race, so it is the publishing that races.
            await first._client()
            await second._client()

            def from_worker(n: int) -> bool:
                return publish_from_worker(make_event(EventType.DOCUMENT_STATUS, "ws-1", {"w": n}))

            results = await asyncio.gather(
                *(first.publish(make_event(EventType.CHAT_MESSAGE, "ws-1")) for _ in range(30)),
                *(second.publish(make_event(EventType.CHAT_MESSAGE, "ws-1")) for _ in range(30)),
                *(asyncio.to_thread(from_worker, n) for n in range(20)),
            )
            assert all(results[60:]), "a worker publish failed"

            logged = [
                Event.from_wire(json.loads(raw))
                for raw in await server.lrange(log_key("ws-1"), 0, -1)
            ]
            assert sorted(e.seq for e in logged) == list(
                range(1, 81)
            ), "a number was reused or skipped"
            assert len({e.epoch for e in logged}) == 1
        finally:
            await shut(first, second)

    async def test_the_log_is_capped_and_only_the_log_expires(self, server, monkeypatch):
        monkeypatch.setattr(settings, "WS_REPLAY_BUFFER_SIZE", 5)
        broker = instance("api-1")
        try:
            for _ in range(8):
                await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
            assert await server.llen(log_key("ws-1")) == 5
            assert [e.seq for e in await broker.replay("ws-1", after_seq=0)] == [4, 5, 6, 7, 8]
            assert 0 < await server.ttl(log_key("ws-1")) <= STATE_TTL_SECONDS
            assert await server.ttl(sequence_key("ws-1")) == -1
            assert await server.ttl(epoch_key("ws-1")) == -1
        finally:
            await shut(broker)

    async def test_losing_the_data_starts_a_new_epoch(self, server):
        broker = instance("api-1")
        try:
            before = await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
            await server.flushdb()
            after = await broker.publish(make_event(EventType.CHAT_MESSAGE, "ws-1"))
            assert after.seq == 1 and after.epoch != before.epoch
        finally:
            await shut(broker)


# ----------------------------------------------------------------- presence --
@pytest.mark.asyncio
class TestPresence:
    async def test_presence_is_one_picture_across_instances(self, server):
        # Enough unrelated keys that SCAN needs many pages to find presence.
        await server.mset({f"unrelated:{n}": n for n in range(1000)})
        first, second = instance("api-1"), instance("api-2")
        try:
            assert await first.presence.arrive("ws-1", "u1", "tab-a") is True
            assert await second.presence.arrive("ws-1", "u1", "tab-b") is False
            assert await second.presence.arrive("ws-1", "u2", "tab-c") is True
            assert await first.presence.online("ws-1") == ["u1", "u2"]
            assert 0 < await server.ttl(presence_key("ws-1", "u1")) <= PRESENCE_TTL_SECONDS

            assert await first.presence.depart("ws-1", "u1", "tab-a") is False
            assert await second.presence.depart("ws-1", "u1", "tab-b") is True
            assert await second.presence.online("ws-1") == ["u2"]
            assert await first.presence.last_seen("ws-1", "u1")
        finally:
            await shut(first, second)


# ------------------------------------------------- through the application --
async def from_another_instance(act):
    other = instance("another-api-instance")
    try:
        return await act(other)
    finally:
        await shut(other)


@pytest.fixture
def application_on_real_redis(client, real_redis):
    """The running application's own broker, subscribed to the real server."""
    broker = get_broker()

    async def attach() -> None:
        await broker.stop()
        broker._unavailable_until = 0.0
        await broker.start()
        await asyncio.wait_for(broker.subscribed.wait(), FAIL_AFTER)

    ws.on_loop(client, attach)
    try:
        yield broker
    finally:

        async def detach() -> None:
            await broker.stop()
            await broker.start()

        # Back to what every other test runs with (conftest disables Redis),
        # so the restart below does not subscribe again.
        with pytest.MonkeyPatch.context() as restore:
            restore.setattr(settings, "REDIS_URL", os.environ.get("REDIS_URL", ""))
            ws.on_loop(client, detach)


class TestThroughTheApplication:
    def test_a_browser_recovers_what_its_instance_missed(
        self, client, owner, application_on_real_redis, real_redis
    ):
        workspace = owner["workspace_id"]
        with ws.open_socket(client, owner["token"], workspace) as socket:
            first = ws.ready(socket)["data"]
            assert real_redis.execute_command("CLIENT", "KILL", "TYPE", "pubsub") == 1
            event = make_event(EventType.DOCUMENT_CREATED, workspace, {"id": "missed"})
            missed = asyncio.run(from_another_instance(lambda other: other.publish(event)))

            again = ws.receive(socket, timeout=RECOVERY)
            assert again["type"] == EventType.CONNECTED, f"expected ready again, got {again}"
            assert (again["data"]["seq"], again["data"]["epoch"]) == (missed.seq, first["epoch"])

            frames, report = ws.replay_after(socket, first["seq"], first["epoch"])
            assert [f["data"]["id"] for f in frames if f["type"] not in ws.PRESENCE] == ["missed"]
            assert report["complete"] is True

    def test_a_logout_on_another_instance_closes_the_socket_here(
        self, client, owner, application_on_real_redis
    ):
        from jose import jwt

        claims = jwt.get_unverified_claims(owner["token"])
        with ws.open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ws.ready(socket)
            asyncio.run(from_another_instance(lambda other: other.revoke_token(claims["jti"])))
            assert ws.close_code(socket) == CLOSE_UNAUTHORIZED
