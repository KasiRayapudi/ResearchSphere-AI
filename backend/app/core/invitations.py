"""
Workspace invitation tokens.

Mirrors app/core/password_reset.py deliberately: a cryptographically random
token is generated, only its SHA-256 is stored, and the raw value is
returned exactly once so it can be emailed. Someone who can read the
database cannot turn its contents back into a workspace membership.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.membership import WorkspaceInvitation

logger = get_logger("invitations")

#: 32 bytes of urandom, URL-safe. Long enough that guessing is not a threat
#: model, short enough to survive being pasted out of an email client.
TOKEN_BYTES = 32

#: Refuse to keep more than this many live invitations for one workspace, so
#: a compromised admin account cannot fill the table.
MAX_PENDING_PER_WORKSPACE = 200


def hash_token(raw_token: str) -> str:
    """SHA-256 of a raw token.

    A fast hash is correct here, unlike for passwords: the token is 256 bits
    of entropy, so there is no dictionary to run against it.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def normalise_email(email: str) -> str:
    """Lower-cased and trimmed, so re-inviting the same person is detected."""
    return (email or "").strip().lower()


def generate_invitation(
    db: Session,
    *,
    workspace_id: str,
    email: str,
    role: str,
    invited_by: str | None,
) -> tuple[str, WorkspaceInvitation]:
    """Create an invitation, returning ``(raw_token, record)``.

    The raw token is returned once, for delivery. Only its hash is stored.
    """
    pending = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.accepted_at.is_(None),
            WorkspaceInvitation.revoked_at.is_(None),
        )
        .count()
    )
    if pending >= MAX_PENDING_PER_WORKSPACE:
        raise ValueError("This workspace has too many pending invitations.")

    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    record = WorkspaceInvitation(
        workspace_id=workspace_id,
        email=normalise_email(email),
        role=role,
        token_hash=hash_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(hours=settings.INVITATION_EXPIRE_HOURS),
        invited_by=invited_by,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return raw_token, record


def find_by_token(db: Session, raw_token: str) -> WorkspaceInvitation | None:
    """The invitation a raw token refers to, whatever state it is in.

    Returns the record even when expired, accepted or revoked, so the caller
    can tell the difference. Nothing here decides whether it may be used.
    """
    if not raw_token:
        return None
    return (
        db.query(WorkspaceInvitation)
        .filter(WorkspaceInvitation.token_hash == hash_token(raw_token))
        .first()
    )


def invitation_message(
    workspace_name: str, inviter_name: str, role: str, token: str
) -> tuple[str, str]:
    """Subject and body for an invitation email.

    The token is included as a value to paste rather than a link: the API
    has no idea what URL the frontend is served from, and guessing one
    produces broken emails.
    """
    subject = f"{inviter_name} invited you to the {workspace_name} workspace"
    body = (
        f"{inviter_name} has invited you to join the ResearchSphere workspace "
        f'"{workspace_name}" as a {role}.\n\n'
        f"Your invitation code is:\n\n    {token}\n\n"
        f"Sign in and enter this code to accept. It expires in "
        f"{settings.INVITATION_EXPIRE_HOURS} hours.\n\n"
        "If you were not expecting this, you can ignore it: nothing happens "
        "until the code is used."
    )
    return subject, body
