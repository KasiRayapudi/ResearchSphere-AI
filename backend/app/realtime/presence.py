"""
Who is currently in a workspace.

Presence is derived from connections rather than declared by clients: a user
is online in a workspace while this deployment holds at least one socket for
them there. A crashed browser is then indistinguishable from a polite
disconnect, which is correct -- both mean the user has gone.

**A user is not one connection.** Two tabs, or a reconnect that overlaps the
socket it replaces, or the same account open on a phone, are all one person
who is online once. Presence is therefore a *set of connection ids* per user
per workspace, and the user is online while that set is non-empty. Treating
it as a flag would mark someone offline the moment they closed one of two
tabs.

The sets live in Redis so every instance sees the same picture, and they
carry a TTL. The TTL is what covers the case no amount of careful cleanup
can: a process that dies without running its own teardown would otherwise
leave its users online forever. Live connections refresh it.

Without Redis this falls back to the connections held by this process, which
is exactly right for a single-process deployment and honestly incomplete for
several. There is no third option that does not invent state.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from app.core.logging import get_logger

logger = get_logger("realtime.presence")

#: How long a presence set survives without a refresh. Comfortably longer
#: than the heartbeat that refreshes it, so an ordinary pause does not make a
#: user flicker offline and back.
PRESENCE_TTL_SECONDS = 90

#: Last-seen outlives presence by a long way: it is only interesting once
#: somebody has gone.
LAST_SEEN_TTL_SECONDS = 30 * 24 * 3600

_PREFIX = "researchsphere:realtime"


def presence_key(workspace_id: str, user_id: str) -> str:
    return f"{_PREFIX}:presence:{workspace_id}:{user_id}"


def presence_pattern(workspace_id: str) -> str:
    return f"{_PREFIX}:presence:{workspace_id}:*"


def last_seen_key(workspace_id: str, user_id: str) -> str:
    return f"{_PREFIX}:lastseen:{workspace_id}:{user_id}"


class PresenceTracker:
    """Presence for one workspace population, shared through Redis."""

    def __init__(self, broker):
        #: The broker owns the Redis connection and its degradation policy.
        #: Duplicating that here would mean two things to keep in step.
        self._broker = broker

    async def _client(self):
        return await self._broker._client()

    async def arrive(self, workspace_id: str, user_id: str, connection_id: str) -> bool:
        """Record a connection. True when this made the user newly online.

        The return value is what decides whether anyone is told: the second
        tab of an already-present user is not an arrival.
        """
        client = await self._client()
        if client is None:
            # Process-local: the manager already holds the connection, so
            # "newly online" is "this is their only one".
            return len(self._broker.manager.connections_for(user_id, workspace_id)) <= 1

        key = presence_key(workspace_id, user_id)
        try:
            added = await client.sadd(key, connection_id)
            await client.expire(key, PRESENCE_TTL_SECONDS)
            size = await client.scard(key)
            return bool(added) and size == 1
        except Exception as exc:
            logger.warning(f"Could not record presence for {user_id}: {exc}")
            return False

    async def refresh(self, workspace_id: str, user_id: str) -> None:
        """Push the expiry out while a connection is still alive."""
        client = await self._client()
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.expire(presence_key(workspace_id, user_id), PRESENCE_TTL_SECONDS)

    async def depart(self, workspace_id: str, user_id: str, connection_id: str) -> bool:
        """Drop a connection. True when the user has no others left."""
        client = await self._client()
        if client is None:
            remaining = len(self._broker.manager.connections_for(user_id, workspace_id))
            if remaining == 0:
                return True
            return False

        key = presence_key(workspace_id, user_id)
        try:
            await client.srem(key, connection_id)
            remaining = await client.scard(key)
            if remaining == 0:
                await client.delete(key)
                await client.set(
                    last_seen_key(workspace_id, user_id),
                    datetime.now(UTC).isoformat(),
                    ex=LAST_SEEN_TTL_SECONDS,
                )
                return True
            return False
        except Exception as exc:
            logger.warning(f"Could not clear presence for {user_id}: {exc}")
            return False

    async def online(self, workspace_id: str) -> list[str]:
        """Every user currently present, across every instance."""
        client = await self._client()
        if client is None:
            return sorted(self._broker.manager.users_in(workspace_id))

        prefix = f"{_PREFIX}:presence:{workspace_id}:"
        found: set[str] = set()
        try:
            # scan_iter rather than KEYS: a blocking KEYS on a shared Redis
            # is a well-known way to stall every other client on it.
            async for key in client.scan_iter(match=presence_pattern(workspace_id), count=100):
                if await client.scard(key):
                    found.add(str(key)[len(prefix) :])
        except Exception as exc:
            logger.warning(f"Could not read presence for {workspace_id}: {exc}")
            return sorted(self._broker.manager.users_in(workspace_id))
        return sorted(found)

    async def last_seen(self, workspace_id: str, user_id: str) -> str | None:
        """When a user was last connected, or None if unknown."""
        client = await self._client()
        if client is None:
            return None
        try:
            return await client.get(last_seen_key(workspace_id, user_id))
        except Exception:
            return None


__all__ = [
    "LAST_SEEN_TTL_SECONDS",
    "PRESENCE_TTL_SECONDS",
    "PresenceTracker",
    "last_seen_key",
    "presence_key",
    "presence_pattern",
]
