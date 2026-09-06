"""
Refresh token issuance, rotation, reuse detection and session revocation.

Rotation with reuse detection is the point of this module: each refresh
consumes the presented token and issues a new one in the same *family*.
Presenting an already-rotated token means either a replay or a stolen token,
and cannot be distinguished from the server side - so the entire family is
revoked, terminating the session for both the attacker and the legitimate user.

Refresh state lives in the database, so this all keeps working when Redis is
unavailable; Redis only accelerates access-token revocation.
"""
import uuid
from datetime import datetime, timedelta
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import create_refresh_token
from app.core.token_store import revoke_token
from app.models.refresh_token import RefreshToken

logger = get_logger("refresh_service")


def issue_refresh_token(
    db: Session,
    user,
    family_id: Optional[str] = None,
    user_agent: Optional[str] = None,
    client_ip: Optional[str] = None,
) -> Tuple[str, RefreshToken]:
    """Create a refresh token and its database record."""
    token = create_refresh_token({"sub": user.id, "role": user.role})

    from app.core.security import decode_token, TOKEN_TYPE_REFRESH

    payload = decode_token(token, expected_type=TOKEN_TYPE_REFRESH)

    record = RefreshToken(
        user_id=user.id,
        jti=payload["jti"],
        family_id=family_id or str(uuid.uuid4()),
        expires_at=datetime.utcnow()
        + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        user_agent=(user_agent or "")[:256] or None,
        client_ip=(client_ip or "")[:64] or None,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return token, record


def revoke_family(db: Session, family_id: str, reason: str) -> int:
    """Revoke every token in a family. Returns how many were revoked."""
    now = datetime.utcnow()
    records = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.family_id == family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .all()
    )
    for record in records:
        record.revoked_at = now
        record.revoked_reason = reason
        revoke_token(record.jti, record.expires_at)
    db.commit()
    return len(records)


def revoke_all_for_user(db: Session, user_id: str, reason: str) -> int:
    """Revoke every active refresh token for a user (full session logout)."""
    now = datetime.utcnow()
    records = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .all()
    )
    for record in records:
        record.revoked_at = now
        record.revoked_reason = reason
        revoke_token(record.jti, record.expires_at)
    db.commit()
    return len(records)


class RefreshResult:
    """Outcome of a rotation attempt."""

    def __init__(
        self,
        ok: bool,
        reason: str = "",
        access_token: str = "",
        refresh_token: str = "",
        user=None,
        reuse_detected: bool = False,
        revoked_count: int = 0,
    ):
        self.ok = ok
        self.reason = reason
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user = user
        self.reuse_detected = reuse_detected
        self.revoked_count = revoked_count


def rotate_refresh_token(
    db: Session,
    presented_token: str,
    user_agent: Optional[str] = None,
    client_ip: Optional[str] = None,
) -> RefreshResult:
    """Validate, rotate and reissue. Detects reuse of a rotated token."""
    from app.core.security import (
        TOKEN_TYPE_REFRESH,
        create_access_token,
        decode_token,
    )
    from app.models.user import User

    try:
        # Skip the Redis blacklist here: rotation blacklists each consumed
        # refresh token, so honouring it would reject a replayed token before
        # the reuse-detection logic below could revoke the family. The
        # refresh_tokens table is the source of truth for these.
        payload = decode_token(
            presented_token,
            expected_type=TOKEN_TYPE_REFRESH,
            check_revocation=False,
        )
    except Exception:
        return RefreshResult(False, reason="invalid_token")

    jti = payload.get("jti")
    record = db.query(RefreshToken).filter(RefreshToken.jti == jti).first()
    if record is None:
        return RefreshResult(False, reason="unknown_token")

    # --- reuse detection -------------------------------------------------
    if record.is_rotated or record.is_revoked:
        revoked = revoke_family(
            db, record.family_id, reason="refresh_token_reuse_detected"
        )
        logger.error(
            f"Refresh token reuse detected for user {record.user_id}; "
            f"revoked {revoked} token(s) in family {record.family_id}."
        )
        return RefreshResult(
            False,
            reason="reuse_detected",
            reuse_detected=True,
            revoked_count=revoked,
        )

    if record.is_expired():
        return RefreshResult(False, reason="expired")

    user = db.query(User).filter(User.id == record.user_id).first()
    if not user or not user.is_active:
        revoke_family(db, record.family_id, reason="user_inactive")
        return RefreshResult(False, reason="user_inactive")

    # --- rotate ----------------------------------------------------------
    new_token, new_record = issue_refresh_token(
        db,
        user,
        family_id=record.family_id,
        user_agent=user_agent,
        client_ip=client_ip,
    )
    record.replaced_by_jti = new_record.jti
    record.last_used_at = datetime.utcnow()
    # The consumed refresh token is blacklisted too, so it cannot be replayed
    # against any component that only checks the blacklist.
    revoke_token(record.jti, record.expires_at)
    db.commit()

    access = create_access_token({"sub": user.id, "role": user.role})
    return RefreshResult(
        True, access_token=access, refresh_token=new_token, user=user
    )
