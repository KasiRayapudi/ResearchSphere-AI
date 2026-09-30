"""
Password reset token service.

Design notes:

* Tokens are generated with ``secrets.token_urlsafe`` (cryptographically
  secure) and only their SHA-256 is persisted, so a database disclosure does
  not let an attacker reset accounts.
* Lookup is by hash, which is why the raw token must carry no user identifier.
* Tokens are single-use and expiring, and a successful reset revokes every
  other outstanding token for that user.
* Verification is constant-time via ``secrets.compare_digest`` on the digest.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.password_reset import PasswordResetToken

logger = get_logger("password_reset")

#: 32 bytes of entropy, url-safe.
TOKEN_BYTES = 32


def hash_token(raw_token: str) -> str:
    return hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()


def generate_reset_token(
    db: Session,
    user_id: str,
    requested_ip: str | None = None,
) -> tuple[str, PasswordResetToken]:
    """Create a reset token, returning ``(raw_token, record)``.

    The raw token is returned exactly once, for delivery to the user. Only its
    hash is stored.
    """
    # Cap outstanding tokens so a request flood cannot fill the table; the
    # oldest active tokens are revoked rather than refusing the user.
    active = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.revoked_at.is_(None),
        )
        .order_by(PasswordResetToken.created_at.asc())
        .all()
    )
    excess = len(active) - (settings.PASSWORD_RESET_MAX_ACTIVE - 1)
    if excess > 0:
        for record in active[:excess]:
            record.revoked_at = datetime.utcnow()

    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    record = PasswordResetToken(
        user_id=user_id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_TTL_MINUTES),
        requested_ip=requested_ip,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return raw_token, record


def verify_reset_token(db: Session, raw_token: str) -> PasswordResetToken | None:
    """Return the usable token record for ``raw_token``, or ``None``.

    ``None`` covers unknown, expired, already-used and revoked tokens alike -
    the caller must not distinguish between them in its response.
    """
    if not raw_token:
        return None

    digest = hash_token(raw_token)
    record = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == digest).first()
    if record is None:
        return None
    # Constant-time comparison on the digest; the DB lookup above is the fast
    # path, this guards against timing analysis on the compare itself.
    if not secrets.compare_digest(record.token_hash, digest):
        return None
    if not record.is_usable():
        return None
    return record


def consume_reset_token(db: Session, record: PasswordResetToken) -> None:
    """Mark a token used and revoke every other outstanding token for the user."""
    now = datetime.utcnow()
    record.used_at = now
    (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id == record.user_id,
            PasswordResetToken.id != record.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.revoked_at.is_(None),
        )
        .update({PasswordResetToken.revoked_at: now}, synchronize_session=False)
    )
    db.commit()


def revoke_all_for_user(db: Session, user_id: str) -> int:
    """Revoke every outstanding token for a user. Returns the count revoked."""
    count = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.revoked_at.is_(None),
        )
        .update({PasswordResetToken.revoked_at: datetime.utcnow()}, synchronize_session=False)
    )
    db.commit()
    return count
