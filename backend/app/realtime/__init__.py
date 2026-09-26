"""
Real-time delivery.

The REST API stays the source of truth for every state change: it owns
authorization, validation and auditing. This package only tells connected
clients that something happened, so a browser stops having to ask.
"""

from .events import PROTOCOL_VERSION, Event, EventType, make_event
from .manager import Connection, ConnectionManager, ConnectionRefused, get_manager, reset_manager

__all__ = [
    "PROTOCOL_VERSION",
    "Connection",
    "ConnectionManager",
    "ConnectionRefused",
    "Event",
    "EventType",
    "get_manager",
    "make_event",
    "reset_manager",
]
