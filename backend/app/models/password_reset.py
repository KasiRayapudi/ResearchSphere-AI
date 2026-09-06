import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String
from sqlalchemy.orm import relationship

from app.core.database import Base


class PasswordResetToken(Base):
    """A single-use, expiring password reset token.

    Only the SHA-256 of the token is stored. The raw token exists solely in
    the reset link sent to the user, so a database disclosure cannot be used
    to reset anyone's password.
    """

    __tablename__ = "password_reset_tokens"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: SHA-256 hex digest of the raw token. Never the token itself.
    token_hash = Column(String(64), nullable=False, unique=True, index=True)

    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    #: Set when the token is invalidated without being used (e.g. superseded
    #: by a newer request, or all tokens revoked after a successful reset).
    revoked_at = Column(DateTime, nullable=True)

    requested_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now: datetime = None) -> bool:
        return (now or datetime.utcnow()) >= self.expires_at

    def is_usable(self, now: datetime = None) -> bool:
        return not self.is_used and not self.is_revoked and not self.is_expired(now)
