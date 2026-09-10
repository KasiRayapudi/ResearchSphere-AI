"""
Workspace membership and invitations.

A workspace used to belong to exactly one person: ``Workspace.owner_id`` was
the entire access model, and every tenant-scoped query compared it to the
caller. These two tables replace that with a real membership list while
leaving ``owner_id`` in place as the denormalised pointer to whoever holds
the owner role, so existing queries and responses keep working.
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.database import Base


class WorkspaceRole(str):
    """Roles within a workspace, most privileged first.

    Distinct from ``User.role``, which is the platform-level role (a site
    admin). A user can be a platform admin and a mere viewer in someone
    else's workspace; the two are checked separately and neither implies
    the other.
    """

    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"

    ALL = ("owner", "admin", "editor", "viewer")

    #: Ordering used for "at least this role" checks.
    RANKS = {"viewer": 10, "editor": 20, "admin": 30, "owner": 40}

    @classmethod
    def rank(cls, role: str | None) -> int:
        """Rank of ``role``; 0 for anything unrecognised.

        An unknown role must never satisfy a requirement, so it sorts below
        viewer rather than raising.
        """
        return cls.RANKS.get((role or "").strip().lower(), 0)

    @classmethod
    def is_valid(cls, role: str | None) -> bool:
        return (role or "").strip().lower() in cls.ALL


class WorkspaceMember(Base):
    """One person's membership of one workspace."""

    __tablename__ = "workspace_members"
    __table_args__ = (
        # One membership row per person per workspace. Without this a second
        # invitation could grant a quietly different role.
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
        # Both directions are queried: "who is in this workspace" for the
        # members page, and "which workspaces can this user see" on every
        # tenant-scoped request.
        Index("ix_workspace_members_workspace_id", "workspace_id"),
        Index("ix_workspace_members_user_id", "user_id"),
        # Exactly one owner per workspace, enforced by the database rather
        # than by remembering to check. Partial indexes are supported by
        # both PostgreSQL and SQLite.
        Index(
            "uq_workspace_single_owner",
            "workspace_id",
            unique=True,
            sqlite_where=Column("role") == "owner",
            postgresql_where=Column("role") == "owner",
        ),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False, default=WorkspaceRole.VIEWER)

    #: Who added them. NULL for the owner of a workspace created before
    #: membership existed, and for a workspace's founding owner.
    invited_by = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    joined_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])
    inviter = relationship("User", foreign_keys=[invited_by])


class WorkspaceInvitation(Base):
    """A pending invitation to join a workspace.

    Addressed to an email rather than a user id: the person being invited
    may not have an account yet. Only the hash of the token is stored, so a
    database read cannot be turned into a workspace membership -- the same
    reasoning as password reset tokens.
    """

    __tablename__ = "workspace_invitations"
    __table_args__ = (
        Index("ix_workspace_invitations_workspace_id", "workspace_id"),
        Index("ix_workspace_invitations_email", "email"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    #: Stored lower-cased so a re-invitation of the same address is
    #: recognised regardless of how it was typed.
    email = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default=WorkspaceRole.VIEWER)

    #: SHA-256 of the raw token. The raw value is shown exactly once, when
    #: the invitation is created, and then only to the transport that
    #: delivers it.
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime, nullable=False)

    #: Terminal states. An invitation is pending only while both are NULL
    #: and it has not expired.
    accepted_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    invited_by = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="invitations")
    inviter = relationship("User", foreign_keys=[invited_by])

    def is_pending(self, now: datetime | None = None) -> bool:
        """True when this invitation can still be accepted."""
        moment = now or datetime.utcnow()
        return self.accepted_at is None and self.revoked_at is None and self.expires_at > moment
