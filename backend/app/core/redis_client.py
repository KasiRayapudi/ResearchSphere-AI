"""
Shared Redis accessor with graceful degradation.

Redis is optional. Every consumer must keep working when it is unreachable,
so this module never raises on connection failure: it returns ``None`` and
records the outage. Availability is re-probed periodically rather than on every
call, so a Redis outage does not add a connection attempt to every request.
"""
import threading
import time
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("redis")

try:  # pragma: no cover - import guard
    import redis as _redis
except Exception:  # pragma: no cover
    _redis = None

#: Seconds to wait before re-probing a Redis that was found unreachable.
RETRY_INTERVAL_SECONDS = 30

_lock = threading.Lock()
_client = None
_unavailable_until = 0.0
_last_error: Optional[str] = None


def _now() -> float:
    return time.monotonic()


def is_configured() -> bool:
    return bool(settings.redis_enabled and _redis is not None)


def get_redis(force: bool = False):
    """Return a live Redis client, or ``None`` when unavailable.

    Never raises. Callers must handle ``None`` by degrading.
    """
    global _client, _unavailable_until, _last_error

    if not is_configured():
        return None

    with _lock:
        if _client is not None:
            return _client
        if not force and _now() < _unavailable_until:
            return None
        try:
            client = _redis.from_url(
                settings.REDIS_URL,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            client.ping()
            _client = client
            _last_error = None
            logger.info("Redis connection established")
            return _client
        except Exception as exc:
            _last_error = str(exc)
            _unavailable_until = _now() + RETRY_INTERVAL_SECONDS
            logger.warning(
                f"Redis unavailable ({exc}); features degrade for "
                f"{RETRY_INTERVAL_SECONDS}s before retrying."
            )
            return None


def mark_unavailable(exc: Exception) -> None:
    """Drop a client that failed mid-operation so the next call re-probes."""
    global _client, _unavailable_until, _last_error
    with _lock:
        _client = None
        _last_error = str(exc)
        _unavailable_until = _now() + RETRY_INTERVAL_SECONDS
    logger.warning(f"Redis operation failed ({exc}); marking unavailable.")


def set_client(client) -> None:
    """Inject a client (used by tests and by the lifespan hook)."""
    global _client, _unavailable_until
    with _lock:
        _client = client
        _unavailable_until = 0.0


def reset() -> None:
    global _client, _unavailable_until, _last_error
    with _lock:
        _client = None
        _unavailable_until = 0.0
        _last_error = None


def status() -> dict:
    return {
        "configured": is_configured(),
        "connected": _client is not None,
        "last_error": _last_error,
    }
