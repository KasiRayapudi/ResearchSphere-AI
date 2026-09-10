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
import uuid
from collections import defaultdict, deque

from app.core.config import settings
from app.core.logging import get_logger

from .events import Event
from .manager import ConnectionManager, get_manager

logger = get_logger("realtime.broker")

#: One channel for every workspace. See the module docstring.
CHANNEL = "researchsphere:realtime:events"


def sequence_key(workspace_id: str) -> str:
    return f"researchsphere:realtime:seq:{workspace_id}"


def log_key(workspace_id: str) -> str:
    return f"researchsphere:realtime:log:{workspace_id}"


#: How long a workspace's replay log and counter survive without activity.
#: Long enough that a client can be away for a while, short enough that a
#: workspace nobody touches stops costing anything.
STATE_TTL_SECONDS = 24 * 3600

#: Backoff bounds for the subscriber when Redis is unreachable. It must keep
#: trying -- a Redis restart should heal on its own -- without hammering.
RECONNECT_MIN_SECONDS = 1
RECONNECT_MAX_SECONDS = 30


class EventBroker:
    """Sequences events, records them for replay, and fans them out."""

    def __init__(self, manager: ConnectionManager | None = None, instance_id: str | None = None):
        self.manager = manager or get_manager()
        #: Identifies this process on the wire so it can ignore its own
        #: messages coming back through pub/sub.
        self.instance_id = instance_id or uuid.uuid4().hex
        self._redis = None
        self._subscriber: asyncio.Task | None = None
        self._running = False

        # In-process fallback, used when Redis is not configured or is down.
        # Correct for a single process, which is the only deployment where
        # running without Redis is a supported choice anyway.
        self._local_seq: dict[str, int] = defaultdict(int)
        self._local_log: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=max(1, settings.WS_REPLAY_BUFFER_SIZE))
        )
        self._local_lock = asyncio.Lock()

        self.stats = {
            "published": 0,
            "received": 0,
            "ignored_own": 0,
            "redis_errors": 0,
            "replayed": 0,
        }

    # ------------------------------------------------------------ lifecycle --
    async def start(self) -> None:
        """Connect to Redis and begin listening. Safe without Redis."""
        self._running = True
        if not self._redis_configured():
            logger.info(
                "Realtime is running without Redis: events reach clients on this "
                "process only. Set REDIS_URL to fan out across instances."
            )
            return
        if self._subscriber is None or self._subscriber.done():
            self._subscriber = asyncio.create_task(self._subscribe_forever())

    async def stop(self) -> None:
        self._running = False
        if self._subscriber is not None:
            self._subscriber.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._subscriber
            self._subscriber = None
        if self._redis is not None:
            with contextlib.suppress(Exception):
                await self._redis.aclose()
            self._redis = None

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
        try:
            import redis.asyncio as aioredis

            client = aioredis.from_url(
                settings.REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=5,
                decode_responses=True,
            )
            await client.ping()
            self._redis = client
            return client
        except Exception as exc:
            self.stats["redis_errors"] += 1
            logger.warning(f"Realtime Redis unavailable ({exc}); staying process-local.")
            self._redis = None
            return None

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

        sequenced = event.with_sequence(await self._next_sequence(event.workspace_id))
        await self._record(sequenced)
        self.stats["published"] += 1

        await self.manager.publish(sequenced)
        await self._fan_out(sequenced)
        return sequenced

    async def _next_sequence(self, workspace_id: str) -> int:
        client = await self._client()
        if client is not None:
            try:
                key = sequence_key(workspace_id)
                value = await client.incr(key)
                await client.expire(key, STATE_TTL_SECONDS)
                return int(value)
            except Exception as exc:
                self.stats["redis_errors"] += 1
                logger.warning(f"Sequence allocation failed ({exc}); using local counter.")
                self._redis = None

        async with self._local_lock:
            self._local_seq[workspace_id] += 1
            return self._local_seq[workspace_id]

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
                self.stats["redis_errors"] += 1
                logger.warning(f"Replay log append failed ({exc}); using local buffer.")
                self._redis = None

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
            self.stats["redis_errors"] += 1
            logger.warning(f"Realtime fan-out failed ({exc}); event stayed process-local.")
            self._redis = None

    # ------------------------------------------------------------- receive --
    async def _subscribe_forever(self) -> None:
        """Listen for events from other instances, reconnecting as needed."""
        delay = RECONNECT_MIN_SECONDS
        while self._running:
            pubsub = None
            try:
                client = await self._client()
                if client is None:
                    raise ConnectionError("Redis unavailable")
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                await pubsub.subscribe(CHANNEL)
                logger.info(f"Realtime subscriber listening on {CHANNEL}")
                delay = RECONNECT_MIN_SECONDS

                async for message in pubsub.listen():
                    if not self._running:
                        break
                    if message.get("type") != "message":
                        continue
                    await self._handle(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats["redis_errors"] += 1
                self._redis = None
                logger.warning(f"Realtime subscriber dropped ({exc}); retrying in {delay}s.")
                await asyncio.sleep(delay)
                # Backing off matters: a Redis that is down stays down for a
                # while, and a tight retry loop turns one outage into two.
                delay = min(RECONNECT_MAX_SECONDS, delay * 2)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()

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

        await self.manager.publish(Event.from_wire(payload))

    # -------------------------------------------------------------- replay --
    async def replay(self, workspace_id: str, after_seq: int, limit: int = 0) -> list[Event]:
        """Events for this workspace newer than ``after_seq``, in order.

        What a client gets after a reconnect. Bounded by the log size, so a
        client that has been away too long receives what is left rather than
        everything -- it can tell, because the first sequence it gets back is
        further ahead than the one it asked for, and refetch instead.
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
                self.stats["redis_errors"] += 1
                logger.warning(f"Replay read failed ({exc}); falling back to local buffer.")
                self._redis = None
                events = []

        if not events:
            async with self._local_lock:
                events = list(self._local_log.get(workspace_id, ()))

        missed = [e for e in events if e.seq > after_seq and e.workspace_id == workspace_id]
        missed.sort(key=lambda e: e.seq)
        self.stats["replayed"] += len(missed)
        return missed[-limit:]

    def snapshot(self) -> dict:
        return {
            "instance": self.instance_id,
            "redis": self._redis is not None,
            "listening": bool(self._subscriber and not self._subscriber.done()),
            "stats": dict(self.stats),
        }


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
        key = sequence_key(event.workspace_id)
        seq = int(client.incr(key))
        client.expire(key, STATE_TTL_SECONDS)

        sequenced = event.with_sequence(seq)
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
    "get_broker",
    "log_key",
    "publish",
    "publish_from_worker",
    "reset_broker",
    "sequence_key",
]
