import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.orm import relationship

from app.core.database import Base


class RefreshToken(Base):
    """A refresh token record supporting rotation and reuse detection.

    Only the token's ``jti`` is stored, never the token string. Rotation links
    each token to its successor via ``replaced_by_jti``; presenting a token
    that has already been rotated is treated as theft and revokes the whole
    family (``family_id``).
    """

    __tablename__ = "refresh_tokens"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: JWT id of this refresh token.
    jti = Column(String(36), nullable=False, unique=True, index=True)
    #: All tokens descended from one login share a family id.
    family_id = Column(String(36), nullable=False, index=True)
    #: Set when this token is rotated; points at its successor.
    replaced_by_jti = Column(String(36), nullable=True)

    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    revoked_reason = Column(String(64), nullable=True)

    user_agent = Column(String(256), nullable=True)
    client_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)

    user = relationship("User")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_rotated(self) -> bool:
        return self.replaced_by_jti is not None

    def is_expired(self, now: datetime = None) -> bool:
        return (now or datetime.utcnow()) >= self.expires_at

    def is_active(self, now: datetime = None) -> bool:
        return not self.is_revoked and not self.is_rotated and not self.is_expired(now)
