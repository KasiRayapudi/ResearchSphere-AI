import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    owner = relationship("User", back_populates="workspaces")
    documents = relationship("Document", back_populates="workspace", cascade="all, delete-orphan")
    chat_sessions = relationship(
        "ChatSession", back_populates="workspace", cascade="all, delete-orphan"
    )
    reports = relationship("Report", back_populates="workspace", cascade="all, delete-orphan")
    connectors = relationship("Connector", back_populates="workspace", cascade="all, delete-orphan")
    #: owner_id remains the denormalised pointer to the owner member, kept so
    #: existing queries and API responses are unchanged. Access control goes
    #: through `members`.
    members = relationship(
        "WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan"
    )
    invitations = relationship(
        "WorkspaceInvitation", back_populates="workspace", cascade="all, delete-orphan"
    )
