"""
Cross-instance event delivery.

The manager only knows the sockets in its own process. Production runs
several API workers behind a load balancer and a Celery worker beside them,
so an event raised in one process has to reach clients connected to another.
Redis pub/sub carries it.

Three things happen when an event is published:

1. **It is sequenced.** ``INCR`` on a per-workspace counter gives a total
   order that every process agrees on, which is what lets a client tell a
   replayed duplicate from something new.
2. **It is appended to a bounded log.** A capped list per workspace, so a
   client that reconnects can ask for what it missed instead of refetching
   everything.
3. **It is fanned out** -- locally to this process's sockets, and over
   pub/sub to the others.

Local delivery happens immediately rather than waiting for the message to
come back around through Redis. That keeps latency low for the common case,
where the process that published is also serving the interested clients, and
it means the whole thing still works with Redis switched off. The cost is
that every message carries the id of the process that sent it, and a
subscriber ignores its own -- otherwise every event would be delivered twice.

**Scaling limit, stated plainly:** there is one channel, and every instance
receives every event and discards the ones for workspaces it is not serving.
That is the right trade at this size -- no subscribe/unsubscribe races as
rooms come and go -- but the cost grows with instances times total event
rate. Per-workspace channels are the upgrade when that starts to matter.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import defaultdict, deque

from app.core.config import settings
from app.core.logging import get_logger

from .events import PROTOCOL_VERSION, Event, EventType
from .manager import Connection, ConnectionManager, get_manager

logger = get_logger("realtime.broker")

#: One channel for every workspace. See the module docstring.
CHANNEL = "researchsphere:realtime:events"

#: A message on the channel that tells every instance to close the sockets
#: opened with one token, rather than carrying an event for clients.
CONTROL_REVOCATION = "revoke_token"


def sequence_key(workspace_id: str) -> str:
    return f"researchsphere:realtime:seq:{workspace_id}"


def log_key(workspace_id: str) -> str:
    return f"researchsphere:realtime:log:{workspace_id}"


def epoch_key(workspace_id: str) -> str:
    return f"researchsphere:realtime:epoch:{workspace_id}"


#: How long a workspace's replay log survives without activity. Long enough
#: that a client can be away for a while, short enough that a workspace
#: nobody touches stops holding a log.
#:
#: The sequence counter and its epoch deliberately do NOT expire. A counter
#: that lapses while a tab sits open in a quiet workspace restarts at 1, and
#: the client -- still holding seq 37 -- would discard every new event as a
#: duplicate. Two small keys per workspace is the price of that not
#: happening; if Redis loses them anyway, the epoch changes with them and
#: clients resynchronise rather than going silently stale.
STATE_TTL_SECONDS = 24 * 3600

#: Backoff bounds for the subscriber when Redis is unreachable. It must keep
#: trying -- a Redis restart should heal on its own -- without hammering.
RECONNECT_MIN_SECONDS = 1
RECONNECT_MAX_SECONDS = 30

#: After a failed connection attempt, how long every caller skips Redis and
#: goes straight to the process-local path. Without a window, each publish,
#: presence update and handshake while Redis is down would pay its own
#: connect timeout. Short, because a Redis that has come back should be
#: used again soon.
UNAVAILABLE_BACKOFF_SECONDS = 5.0

#: Timeouts for the commands on the realtime path. Each is one small round
#: trip, so a command that takes seconds means Redis is in trouble and the
#: caller is better off falling back.
CONNECT_TIMEOUT_SECONDS = 2
COMMAND_TIMEOUT_SECONDS = 5

#: The subscription reads in slices this long. A slice that ends with nothing
#: means only that the channel was quiet -- see ``EventBroker._listen``.
SUBSCRIBER_POLL_SECONDS = 1.0

#: A subscription that has heard nothing for SUBSCRIBER_PING_SECONDS pings
#: Redis, and one whose ping goes unanswered for
#: SUBSCRIBER_REPLY_TIMEOUT_SECONDS is given up as dead. The interval is well
#: under the idle timeouts of common load balancers and NAT gateways, which
#: drop a silent connection without telling either end.
SUBSCRIBER_PING_SECONDS = 15.0
SUBSCRIBER_REPLY_TIMEOUT_SECONDS = 5.0


class EventBroker:
    """Sequences events, records them for replay, and fans them out."""

    def __init__(self, manager: ConnectionManager | None = None, instance_id: str | None = None):
        #: Resolved on use unless one was given. Capturing get_manager() at
        #: construction pinned the broker to whichever manager existed then,
        #: so replacing the process manager left the broker delivering into a
        #: room nobody was in.
        self._manager = manager
        #: Identifies this process on the wire so it can ignore its own
        #: messages coming back through pub/sub.
        self.instance_id = instance_id or uuid.uuid4().hex
        self._redis = None
        #: Monotonic time before which no connection attempt is made.
        self._unavailable_until = 0.0
        self._subscriber: asyncio.Task | None = None
        self._running = False
        #: Set while subscribed to the channel, cleared while not. Anything
        #: that depends on cross-instance delivery -- readiness, a test --
        #: waits on this instead of guessing how long a subscription takes.
        self.subscribed = asyncio.Event()
        #: Set whenever the subscriber moves -- subscribed, lost, pinged,
        #: re-announced -- or a Redis call fails. Something watching it, a
        #: health check or a test, clears it and waits for the next change
        #: instead of polling for one.
        self.changed = asyncio.Event()
        #: Clients being closed in the background; see ``_discard``.
        self._closers: set[asyncio.Task] = set()
        #: The loop this broker was started on. Captured so a thread that is
        #: not the loop -- inline ingestion runs in one -- can hand work back
        #: to it. None in a process with no loop at all, such as a real
        #: Celery worker.
        self.loop: asyncio.AbstractEventLoop | None = None

        # In-process fallback, used when Redis is not configured or is down.
        # Correct for a single process, which is the only deployment where
        # running without Redis is a supported choice anyway.
        self._local_seq: dict[str, int] = defaultdict(int)
        self._local_log: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max(1, settings.WS_REPLAY_BUFFER_SIZE))
        )
        self._local_lock = asyncio.Lock()

        # Presence shares the broker's Redis connection and its degradation
        # policy rather than opening a second one with rules of its own.
        from .presence import PresenceTracker

        self.presence = PresenceTracker(self)

        self.stats = {
            "published": 0,
            "received": 0,
            "ignored_own": 0,
            "redis_errors": 0,
            "replayed": 0,
            # More than one subscription in a process's life means it lost
            # one: each is a window in which other instances went unheard.
            "subscriptions": 0,
            "pings": 0,
            "reannounced": 0,
        }

    @property
    def manager(self) -> ConnectionManager:
        return self._manager or get_manager()

    def _tally(self, stat: str, by: int = 1) -> None:
        self.stats[stat] += by
        self.changed.set()

    def _lost_redis(self, exc: Exception, attempt: str, fallback: str, client=None) -> None:
        """A Redis call failed: fall back, and leave Redis alone for a while.

        ``client`` is the one the call failed on. It is closed, and dropped
        if it is still the broker's current client -- not if another call has
        already replaced it with a working one -- so the next attempt starts
        on a fresh connection. The same window a failed connect opens is
        opened here. Without it a Redis that has stopped answering costs
        every call its full command timeout, and a handshake makes several:
        each new socket would stall for many seconds instead of one call
        paying once per window.
        """
        failed = client if client is not None else self._redis
        if self._redis is failed:
            self._redis = None
        self._discard(failed)
        self._tally("redis_errors")
        self._unavailable_until = time.monotonic() + UNAVAILABLE_BACKOFF_SECONDS
        logger.warning(f"{attempt} failed ({exc}); {fallback}.")

    def _discard(self, client) -> None:
        """Close a client this broker has stopped using.

        Dropping the reference was not enough: its pooled connections stayed
        open until garbage collection, and one collected after its event loop
        had closed raised "Event loop is closed" on the way out. The close
        runs in the background so the failing call is not held up by it, and
        ``stop`` waits for any still running.
        """
        if client is None:
            return

        async def close() -> None:
            with contextlib.suppress(Exception):
                # The pool explicitly: aclose() alone closes it only for a
                # client flagged as owning it, and this broker owns every
                # client it discards.
                await client.aclose(close_connection_pool=True)

        closer = asyncio.ensure_future(close())
        self._closers.add(closer)
        closer.add_done_callback(self._closers.discard)

    # ------------------------------------------------------------ lifecycle --
    async def start(self) -> None:
        """Connect to Redis and begin listening. Safe without Redis.

        Owned by the loop that starts it, like the manager: a start from
        another loop leaves a live owner alone, and takes over only from one
        that has died -- dropping the Redis client and subscriber bound to it.
        """
        current = asyncio.get_running_loop()
        if self.loop is not None and self.loop is not current:
            if not self.loop.is_closed() and self.loop.is_running():
                logger.warning(
                    "Realtime broker is already running on another event loop; leaving it there"
                )
                return
            self._subscriber = None
            self._redis = None
            self.subscribed.clear()
        self._running = True
        self.loop = current
        if not self._redis_configured():
            logger.info(
                "Realtime is running without Redis: events reach clients on this "
                "process only. Set REDIS_URL to fan out across instances."
            )
            return
        if self._subscriber is None or self._subscriber.done():
            self._subscriber = asyncio.create_task(self._subscribe_forever())

    async def stop(self) -> None:
        # Only the owning loop stops the broker. From another loop this would
        # clear the loop worker threads hand events to, and every one after
        # that would be dropped.
        if self.loop is not None and self.loop is not asyncio.get_running_loop():
            return
        self._running = False
        self.loop = None
        self.subscribed.clear()
        if self._subscriber is not None:
            self._subscriber.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscriber
            self._subscriber = None
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose(close_connection_pool=True)
            self._redis = None
        if self._closers:
            # Clients given up on earlier and still closing: nothing this
            # broker opened may outlive it.
            await asyncio.wait(list(self._closers), timeout=CONNECT_TIMEOUT_SECONDS + 1)

    @staticmethod
    def _redis_configured() -> bool:
        return bool(settings.redis_enabled)

    async def _client(self):
        """A live async Redis client, or None.

        Never raises: the realtime layer degrades to this process when Redis
        is unavailable, exactly as the rest of the application does.
        """
        if self._redis is not None:
            return self._redis
        if not self._redis_configured():
            return None
        if time.monotonic() < self._unavailable_until:
            return None
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(
                settings.REDIS_URL,
                socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
                socket_timeout=COMMAND_TIMEOUT_SECONDS,
                decode_responses=True,
            )
        except Exception as exc:
            self._unreachable(exc)
            return None
        try:
            await client.ping()
        except Exception as exc:
            # A client whose first command failed may still hold a socket.
            self._discard(client)
            self._unreachable(exc)
            return None
        self._redis = client
        return client

    def _unreachable(self, exc: Exception) -> None:
        self._tally("redis_errors")
        self._unavailable_until = time.monotonic() + UNAVAILABLE_BACKOFF_SECONDS
        logger.warning(
            f"Realtime Redis unavailable ({exc}); staying process-local for "
            f"{UNAVAILABLE_BACKOFF_SECONDS:.0f}s."
        )
        self._redis = None

    # ------------------------------------------------------------- publish --
    async def publish(self, event: Event) -> Event:
        """Sequence, record and deliver one event. Returns the sequenced copy.

        Delivery to this process's own sockets happens here rather than on
        the way back from Redis, so an event is not delayed by a round trip
        and does not disappear when Redis does.
        """
        if not event.workspace_id:
            logger.error(f"Refusing to publish {event.type} with no workspace")
            return event

        seq, epoch = await self._next_sequence(event.workspace_id)
        sequenced = event.with_sequence(seq, epoch)
        await self._record(sequenced)
        self.stats["published"] += 1

        await self.manager.publish(sequenced)
        await self._fan_out(sequenced)
        return sequenced

    async def _next_sequence(self, workspace_id: str) -> tuple[int, str]:
        """The next sequence number for a workspace, and the epoch it belongs to.

        One MULTI/EXEC round trip: increment, create the epoch if this is the
        first event it has ever seen, read it back. SET NX means concurrent
        publishers converge on whichever epoch was written first.
        """
        client = await self._client()
        if client is not None:
            try:
                pipe = client.pipeline()
                pipe.incr(sequence_key(workspace_id))
                pipe.set(epoch_key(workspace_id), uuid.uuid4().hex, nx=True)
                pipe.get(epoch_key(workspace_id))
                seq, _, epoch = await pipe.execute()
                return int(seq), str(epoch)
            except Exception as exc:
                self._lost_redis(exc, "Sequence allocation", "using the local counter", client)

        # Process-local: the epoch is this process, so a restart -- including
        # every dev-server reload -- is visible to clients as an epoch change.
        async with self._local_lock:
            self._local_seq[workspace_id] += 1
            return self._local_seq[workspace_id], self.instance_id

    async def current_position(self, workspace_id: str) -> tuple[int, str]:
        """The latest sequence issued for a workspace and its epoch.

        Sent to a client when it connects, so a socket that receives nothing
        still knows where it stands and can resume from the right place.
        """
        client = await self._client()
        if client is not None:
            try:
                pipe = client.pipeline()
                pipe.get(sequence_key(workspace_id))
                pipe.set(epoch_key(workspace_id), uuid.uuid4().hex, nx=True)
                pipe.get(epoch_key(workspace_id))
                seq, _, epoch = await pipe.execute()
                return int(seq or 0), str(epoch)
            except Exception as exc:
                self._lost_redis(exc, "Position lookup", "using the local counter", client)

        async with self._local_lock:
            return self._local_seq.get(workspace_id, 0), self.instance_id

    async def oldest_retained(self, workspace_id: str) -> int:
        """Sequence of the oldest event still in the replay log, or 0 if empty."""
        client = await self._client()
        if client is not None:
            try:
                raw = await client.lindex(log_key(workspace_id), 0)
                if not raw:
                    return 0
                return Event.from_wire(json.loads(raw)).seq
            except Exception as exc:
                self._lost_redis(exc, "Replay log read", "using the local buffer", client)

        async with self._local_lock:
            log = self._local_log.get(workspace_id)
            return log[0].seq if log else 0

    async def _record(self, event: Event) -> None:
        """Append to the workspace's replay log, oldest entries falling off."""
        client = await self._client()
        if client is not None:
            try:
                key = log_key(event.workspace_id)
                pipe = client.pipeline()
                pipe.rpush(key, json.dumps(event.to_wire()))
                pipe.ltrim(key, -max(1, settings.WS_REPLAY_BUFFER_SIZE), -1)
                pipe.expire(key, STATE_TTL_SECONDS)
                await pipe.execute()
                return
            except Exception as exc:
                self._lost_redis(exc, "Replay log append", "using the local buffer", client)

        async with self._local_lock:
            self._local_log[event.workspace_id].append(event)

    async def _fan_out(self, event: Event) -> None:
        """Send to the other instances."""
        client = await self._client()
        if client is None:
            return
        try:
            payload = event.to_wire()
            payload["origin"] = self.instance_id
            await client.publish(CHANNEL, json.dumps(payload))
        except Exception as exc:
            self._lost_redis(exc, "Realtime fan-out", "the event stayed process-local", client)

    async def revoke_token(self, token_id: str) -> int:
        """Close every socket, on every instance, that opened with this token.

        Called when a token is revoked -- a logout -- so its sockets go at
        once. Otherwise they would last until the next re-validation sweep,
        up to a minute, while REST already refuses the same token. Returns
        how many sockets this instance closed; the others close theirs when
        the message reaches them, and the sweep remains the backstop for an
        instance it never reaches.
        """
        if not token_id:
            return 0
        closed = await self.manager.close_token(token_id)
        client = await self._client()
        if client is not None:
            try:
                await client.publish(
                    CHANNEL,
                    json.dumps(
                        {
                            "control": CONTROL_REVOCATION,
                            "token_id": token_id,
                            "origin": self.instance_id,
                        }
                    ),
                )
            except Exception as exc:
                self._lost_redis(
                    exc, "Revocation fan-out", "other instances close at their next sweep", client
                )
        return closed

    # ------------------------------------------------------------- receive --
    async def _subscribe_forever(self) -> None:
        """Listen for events from other instances, reconnecting as needed."""
        delay = RECONNECT_MIN_SECONDS
        # Whether this instance has been deaf to the others at some point
        # since it started: the subscription would not come up, or came up
        # and was lost. Sockets it served meanwhile must be told.
        deaf = False
        while self._running:
            pubsub = None
            failure: Exception | None = None
            try:
                client = await self._client()
                if client is None:
                    raise ConnectionError("Redis unavailable")
                pubsub = client.pubsub()
                await pubsub.subscribe(CHANNEL)
                await self._confirmed(pubsub)
                self.subscribed.set()
                self._tally("subscriptions")
                logger.info(f"Realtime subscriber listening on {CHANNEL}")
                delay = RECONNECT_MIN_SECONDS
                if deaf:
                    await self._reannounce()
                    deaf = False
                await self._listen(pubsub)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure = exc
            finally:
                # Before any backoff: a subscription given up on must not
                # stay registered on the server while this waits to replace
                # it, still being sent messages nobody reads.
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()
            if failure is None:
                continue  # _listen returned: the broker is stopping
            deaf = True
            self.subscribed.clear()
            dropped, self._redis = self._redis, None
            self._discard(dropped)
            self._tally("redis_errors")
            logger.warning(f"Realtime subscriber dropped ({failure}); retrying in {delay}s.")
            # Backing off matters: a Redis that is down stays down for a
            # while, and a tight retry loop turns one outage into two.
            await self._pause(delay)
            delay = min(RECONNECT_MAX_SECONDS, delay * 2)

    async def _pause(self, seconds: float) -> None:
        """Wait out one reconnect backoff.

        A method of its own so the schedule can be observed and driven: a
        test replaces it to see each delay the subscriber asks for, and to
        stop the broker part-way through one, without waiting on the clock.
        """
        await asyncio.sleep(seconds)

    async def _confirmed(self, pubsub) -> None:
        """Wait for Redis to confirm the subscription.

        SUBSCRIBE has no reply in the usual sense: redis-py writes it and
        returns. Until the confirmation comes back, an event published by
        another instance may or may not reach this one, so neither the
        ``subscribed`` signal nor a re-announcement may go out before it.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SUBSCRIBER_REPLY_TIMEOUT_SECONDS
        while (remaining := deadline - loop.time()) > 0:
            message = await pubsub.get_message(timeout=remaining)
            if message is not None and message.get("type") == "subscribe":
                return
        raise ConnectionError("Redis did not confirm the subscription")

    async def _listen(self, pubsub) -> None:
        """Deliver what arrives until the connection fails or the broker stops.

        Not ``pubsub.listen()``. That reads under the client's command
        timeout, and a channel is silent for as long as nobody publishes, so
        against a real Redis a quiet deployment tore its subscription down
        every few seconds and lost whatever other instances published while
        it came back. Reads here wait in bounded slices, and a slice that
        ends empty means only that nothing was sent.

        That timeout was also, by accident, the only thing that noticed a
        dead connection, so silence is now tested on purpose: after
        SUBSCRIBER_PING_SECONDS with nothing heard Redis is pinged, and a ping
        unanswered for SUBSCRIBER_REPLY_TIMEOUT_SECONDS ends the connection.
        """
        loop = asyncio.get_running_loop()
        heard = loop.time()
        pinged_at: float | None = None
        while self._running:
            message = await pubsub.get_message(timeout=SUBSCRIBER_POLL_SECONDS)
            now = loop.time()
            if message is not None:
                # Anything at all -- an event, a pong -- proves the connection.
                heard, pinged_at = now, None
                if message.get("type") == "message":
                    await self._handle(message.get("data"))
                continue
            if pinged_at is not None:
                if now - pinged_at >= SUBSCRIBER_REPLY_TIMEOUT_SECONDS:
                    raise ConnectionError(f"Redis has not answered for {now - heard:.1f}s")
            elif now - heard >= SUBSCRIBER_PING_SECONDS:
                await pubsub.ping()
                self._tally("pings")
                pinged_at = now

    async def _reannounce(self) -> int:
        """Tell every socket on this instance where its workspace stands now.

        Runs when a subscription comes up after a period without one.
        Whatever other instances published in between never arrived here,
        and nothing downstream could tell: a client does not look for gaps,
        because restricted events leave gaps by design. A fresh
        ``connection.ready`` is the prompt a client already acts on -- in the
        same epoch it resumes from a little behind what it has seen and the
        replay log fills the gap; in a new one it refetches. The subscription
        is confirmed before this runs, so anything published after the
        position read here is delivered live.

        Returns how many sockets were told. Never raises: a failure here must
        not take down the subscription it follows.
        """
        rooms: dict[str, list[Connection]] = defaultdict(list)
        for connection in self.manager.live():
            rooms[connection.workspace_id].append(connection)

        told = 0
        for workspace_id, connections in rooms.items():
            try:
                online = await self.presence.online(workspace_id)
                seq, epoch = await self.current_position(workspace_id)
                for connection in connections:
                    frame = ready_event(connection, online=online, seq=seq, epoch=epoch)
                    if await self.manager.send_to(connection, frame):
                        told += 1
            except Exception as exc:
                logger.warning(f"Could not re-announce to workspace {workspace_id}: {exc}")

        self._tally("reannounced", told)
        if told:
            logger.info(f"Realtime subscription restored; told {told} socket(s) to resume")
        return told

    async def _handle(self, raw) -> None:
        """Deliver one event that arrived from another instance."""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Discarding malformed realtime message")
            return
        if not isinstance(payload, dict):
            logger.warning("Discarding realtime message that is not an object")
            return

        self.stats["received"] += 1
        if payload.get("origin") == self.instance_id:
            # Already delivered locally at publish time.
            self.stats["ignored_own"] += 1
            return

        if "control" in payload:
            # An instruction to this instance, never an event for a client.
            # Events cannot collide: their envelope has no such key.
            await self._control(payload)
            return

        await self.manager.publish(Event.from_wire(payload))

    async def _control(self, payload: dict) -> None:
        if payload.get("control") == CONTROL_REVOCATION and payload.get("token_id"):
            await self.manager.close_token(str(payload["token_id"]))
            return
        logger.warning(f"Ignoring unknown realtime control message {payload.get('control')!r}")

    # -------------------------------------------------------------- replay --
    async def replay(
        self, workspace_id: str, after_seq: int, limit: int = 0, epoch: str | None = None
    ) -> list[Event]:
        """Events for this workspace newer than ``after_seq``, in order.

        What a client gets after a reconnect. Bounded by the log size, so a
        client that has been away too long receives only what is left.
        Whether that was everything is answered by the caller comparing
        ``oldest_retained`` with the position asked for -- not by looking for
        gaps, because restricted events legitimately leave gaps in what any
        one client sees.

        ``epoch`` limits the replay to one sequence space; numbers from
        different epochs are not comparable.
        """
        limit = limit or max(1, settings.WS_REPLAY_BUFFER_SIZE)
        events: list[Event] = []

        client = await self._client()
        if client is not None:
            try:
                raw_entries = await client.lrange(log_key(workspace_id), -limit, -1)
                for raw in raw_entries:
                    with contextlib.suppress(TypeError, ValueError):
                        events.append(Event.from_wire(json.loads(raw)))
            except Exception as exc:
                self._lost_redis(exc, "Replay read", "falling back to the local buffer", client)
                events = []

        if not events:
            async with self._local_lock:
                events = list(self._local_log.get(workspace_id, ()))

        missed = [
            e
            for e in events
            if e.seq > after_seq
            and e.workspace_id == workspace_id
            and (epoch is None or e.epoch == epoch)
        ]
        missed.sort(key=lambda e: e.seq)
        self.stats["replayed"] += len(missed)
        return missed[-limit:]

    def snapshot(self) -> dict:
        return {
            "instance": self.instance_id,
            "redis": self._redis is not None,
            "listening": self.subscribed.is_set(),
            "stats": dict(self.stats),
        }


def ready_event(connection: Connection, *, online: list[str], seq: int, epoch: str) -> Event:
    """The ``connection.ready`` frame: who this socket is, and where its workspace stands.

    Sent when a socket opens, and again after this instance may have missed
    what the others published (``EventBroker._reannounce``). ``seq`` and
    ``epoch`` are what a client resumes from; ``online`` replaces its
    presence list, which may have missed changes too.
    """
    return Event(
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
    )


_broker: EventBroker | None = None


def get_broker() -> EventBroker:
    """The broker for this process."""
    global _broker
    if _broker is None:
        _broker = EventBroker()
    return _broker


def reset_broker() -> None:
    global _broker
    _broker = None


async def publish(event: Event) -> Event:
    """Publish from async code."""
    return await get_broker().publish(event)


def publish_from_worker(event: Event) -> bool:
    """Publish from a synchronous process, i.e. the Celery worker.

    The worker has no event loop and no WebSocket connections of its own, so
    it does the same sequencing and logging with the blocking Redis client
    and lets the API instances do the delivering.

    Returns whether the event was handed to Redis. False means no client will
    see it: without a broker there is no worker either, so in that
    configuration ingestion runs inline in the API process and publishes
    through the async path instead.
    """
    from app.core.redis_client import get_redis, mark_unavailable

    if not event.workspace_id:
        logger.error(f"Refusing to publish {event.type} with no workspace")
        return False

    client = get_redis()
    if client is None:
        return False

    try:
        allocate = client.pipeline()
        allocate.incr(sequence_key(event.workspace_id))
        allocate.set(epoch_key(event.workspace_id), uuid.uuid4().hex, nx=True)
        allocate.get(epoch_key(event.workspace_id))
        seq, _, epoch = allocate.execute()

        sequenced = event.with_sequence(int(seq), str(epoch))
        payload = sequenced.to_wire()
        # No origin: this process serves no sockets, so every subscriber
        # should deliver it rather than mistake it for its own echo.
        payload["origin"] = None

        entry = json.dumps(sequenced.to_wire())
        pipe = client.pipeline()
        pipe.rpush(log_key(event.workspace_id), entry)
        pipe.ltrim(log_key(event.workspace_id), -max(1, settings.WS_REPLAY_BUFFER_SIZE), -1)
        pipe.expire(log_key(event.workspace_id), STATE_TTL_SECONDS)
        pipe.publish(CHANNEL, json.dumps(payload))
        pipe.execute()
        return True
    except Exception as exc:
        # A dropped notification must never fail the ingestion that raised
        # it: the document is still processed, the client just finds out by
        # asking instead of being told.
        mark_unavailable(exc)
        logger.warning(f"Worker could not publish {event.type}: {exc}")
        return False


__all__ = [
    "CHANNEL",
    "EventBroker",
    "epoch_key",
    "get_broker",
    "log_key",
    "publish",
    "publish_from_worker",
    "ready_event",
    "reset_broker",
    "sequence_key",
]
