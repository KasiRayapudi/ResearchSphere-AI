"""
Token revocation store.

Two layers, deliberately:

* **Redis** holds the blacklist of revoked ``jti`` values with a TTL matching
  the token's remaining lifetime, so entries expire themselves. This is the
  fast path consulted on every request.
* **The database** holds refresh-token records, which are the source of truth
  for rotation and reuse detection and survive a Redis flush.

Degradation: when Redis is unavailable, access-token revocation checks fail
**open** (the request proceeds). Failing closed would turn a Redis outage into
a total authentication outage. Refresh tokens are unaffected, because their
state lives in the database - so a stolen refresh token is still detected and
a compromised session can still be terminated even with Redis down.
"""
from datetime import datetime, timezone
from typing import Optional

from app.core.logging import get_logger
from app.core.redis_client import get_redis, mark_unavailable

logger = get_logger("token_store")

REVOKED_PREFIX = "revoked_jti:"
#: Fallback TTL when a token carries no usable expiry.
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def _ttl_from_exp(exp) -> int:
    """Seconds until ``exp``, floored at 1."""
    if not exp:
        return DEFAULT_TTL_SECONDS
    try:
        if isinstance(exp, (int, float)):
            expiry = datetime.fromtimestamp(exp, tz=timezone.utc)
        else:
            expiry = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
        remaining = int((expiry - datetime.now(timezone.utc)).total_seconds())
        return max(1, remaining)
    except Exception:
        return DEFAULT_TTL_SECONDS


def revoke_token(jti: str, exp=None) -> bool:
    """Blacklist a ``jti``. Returns True when it was recorded in Redis."""
    if not jti:
        return False
    client = get_redis()
    if client is None:
        logger.warning(
            f"Redis unavailable; access token {jti} could not be blacklisted. "
            "Its refresh token is still revoked in the database."
        )
        return False
    try:
        client.setex(f"{REVOKED_PREFIX}{jti}", _ttl_from_exp(exp), "1")
        return True
    except Exception as exc:
        mark_unavailable(exc)
        return False


def is_token_revoked(jti: str) -> bool:
    """Check the blacklist.

    Fails **open** when Redis is unavailable: returning True would lock every
    user out during a Redis outage.
    """
    if not jti:
        return False
    client = get_redis()
    if client is None:
        return False
    try:
        return client.exists(f"{REVOKED_PREFIX}{jti}") > 0
    except Exception as exc:
        mark_unavailable(exc)
        return False


def revoke_many(jtis, exp=None) -> int:
    return sum(1 for jti in jtis if revoke_token(jti, exp))


def blacklist_size() -> Optional[int]:
    """Number of blacklisted tokens, or None when Redis is unavailable."""
    client = get_redis()
    if client is None:
        return None
    try:
        return len(list(client.scan_iter(match=f"{REVOKED_PREFIX}*", count=1000)))
    except Exception as exc:
        mark_unavailable(exc)
        return None
