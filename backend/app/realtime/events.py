"""
The event vocabulary.

Everything pushed to a browser goes through this envelope. Two properties
matter more than the shape itself:

**Ordering.** Each workspace has a monotonic sequence number. A client
applies an event only if its sequence is higher than the last one applied,
which makes duplicates harmless -- and duplicates *will* happen, because a
reconnecting client replays from its last known sequence and the replay
window necessarily overlaps with whatever arrived live in the meantime.

**Isolation.** Every event carries the workspace it belongs to, and the
delivery layer refuses to hand an event to a connection subscribed to a
different one. Tenancy is not a property of the caller remembering to filter.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy import Column

    #: An id as callers hold it. The models declare their columns in
    #: SQLAlchemy's classic style, so an attribute read off a row is typed
    #: Column[str] even though its runtime value is a str. make_event coerces
    #: every id with str(), so accepting either is exact rather than lax --
    #: and None is still refused.
    RowId = str | Column[str]
else:
    RowId = str

#: Bumped when the envelope changes shape in a way an old client cannot read.
#: The client sends the version it speaks and the server refuses a mismatch,
#: so a stale tab fails loudly at connect rather than silently misreading
#: events for the rest of its session.
PROTOCOL_VERSION = 1


class EventType:
    """Every event this server can emit.

    A closed vocabulary rather than free-form strings: the frontend switches
    on these, and a typo in a publisher would otherwise become an event
    nobody receives and nobody notices.
    """

    # --- documents ---
    DOCUMENT_CREATED = "document.created"
    DOCUMENT_STATUS = "document.status"
    DOCUMENT_DELETED = "document.deleted"

    # --- workspace collaboration ---
    MEMBER_ADDED = "member.added"
    MEMBER_REMOVED = "member.removed"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    INVITATION_SENT = "invitation.sent"
    INVITATION_REVOKED = "invitation.revoked"
    INVITATION_ACCEPTED = "invitation.accepted"

    # --- chat ---
    CHAT_CREATED = "chat.created"
    CHAT_RENAMED = "chat.renamed"
    CHAT_DELETED = "chat.deleted"
    CHAT_MESSAGE = "chat.message"

    # --- presence ---
    PRESENCE_ONLINE = "presence.online"
    PRESENCE_OFFLINE = "presence.offline"

    #: Connection-level frames. Not workspace events: they never carry a
    #: sequence number and are never replayed.
    CONNECTED = "connection.ready"
    PONG = "connection.pong"
    ERROR = "connection.error"
    #: Answer to a resume: how much was replayed and whether that was
    #: everything, so a client knows whether it must refetch.
    RESUMED = "connection.resumed"


@dataclass(frozen=True)
class Event:
    """One thing that happened, addressed to one workspace.

    ``recipient_id`` and ``permission`` narrow the audience *within* that
    workspace, and both are enforced by the delivery layer rather than left
    to publishers. They exist because not everything in a workspace is
    visible to everyone in it: chat sessions belong to the person who had
    them, and pending invitations are readable only by members who may
    invite. Broadcasting either to the whole room would hand out over the
    socket what the REST API refuses to return.
    """

    type: str
    workspace_id: str
    data: dict[str, Any] = field(default_factory=dict)
    #: Assigned by the broker at publish time, not by the caller. 0 means
    #: "not sequenced" and is used only for connection-level frames.
    seq: int = 0
    #: Which sequence ``seq`` belongs to. A counter can restart -- a process
    #: without Redis restarts it on every reload, and Redis can lose data --
    #: and a client comparing a new seq 1 against its remembered seq 37 would
    #: otherwise discard every event as a duplicate. A changed epoch tells it
    #: to drop its position and resynchronise instead.
    epoch: str = ""
    ts: str = ""
    #: The user whose action caused this, when there was one. Lets a client
    #: skip an echo of its own change, which it has already applied
    #: optimistically.
    actor_id: str | None = None
    #: Deliver only to this user's connections.
    recipient_id: str | None = None
    #: Deliver only to connections whose workspace role grants this
    #: permission, as named in app.core.workspace_access.PERMISSIONS.
    permission: str | None = None

    def with_sequence(self, seq: int, epoch: str = "") -> Event:
        return replace(
            self,
            seq=seq,
            epoch=epoch or self.epoch,
            ts=self.ts or datetime.now(UTC).isoformat(),
        )

    def to_wire(self) -> dict:
        payload = asdict(self)
        payload["v"] = PROTOCOL_VERSION
        return payload

    @staticmethod
    def from_wire(payload: dict) -> Event:
        """Rebuild an event that arrived over Redis.

        Unknown keys are dropped rather than passed through: what crosses
        pub/sub is written by another process of this application, but it is
        still input, and an envelope is not a place to be generous.
        """
        return Event(
            type=str(payload.get("type", "")),
            workspace_id=str(payload.get("workspace_id", "")),
            data=payload.get("data") or {},
            seq=int(payload.get("seq") or 0),
            epoch=str(payload.get("epoch") or ""),
            ts=str(payload.get("ts") or ""),
            actor_id=payload.get("actor_id"),
            recipient_id=payload.get("recipient_id"),
            permission=payload.get("permission"),
        )


def make_event(
    event_type: str,
    workspace_id: RowId,
    data: dict | None = None,
    actor_id: RowId | None = None,
    *,
    recipient_id: RowId | None = None,
    permission: str | None = None,
) -> Event:
    """Build an unsequenced event, timestamped now."""
    return Event(
        type=event_type,
        workspace_id=str(workspace_id),
        data=data or {},
        ts=datetime.now(UTC).isoformat(),
        actor_id=str(actor_id) if actor_id else None,
        recipient_id=str(recipient_id) if recipient_id else None,
        permission=permission or None,
    )


def monotonic_ms() -> int:
    """Milliseconds from a clock that cannot go backwards.

    Used for heartbeat deadlines. time.time() would let an NTP correction
    either kill healthy connections or keep dead ones alive.
    """
    return int(time.monotonic() * 1000)
