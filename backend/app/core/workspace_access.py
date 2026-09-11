"""
Workspace authorization.

Every tenant-scoped request goes through ``resolve_workspace`` in
``app.core.security``, which delegates the membership lookup here. Routes
that need more than "is a member" call ``require_workspace_role`` afterwards.

Two rules keep this safe:

* Nothing outside this module compares ``Workspace.owner_id`` to the caller.
  Scattering that check across routers is how the IDOR bugs got in.
* A caller's role is read from the database on every request, never from the
  token. A membership removed a second ago must not survive in a JWT that is
  valid for another hour.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.audit import AuditAction, AuditOutcome, audit
from app.models.membership import WorkspaceMember, WorkspaceRole

#: What each role may do. Written once, here, so a permission question has
#: exactly one answer and routes never re-derive it.
#:
#:   owner   everything, including deleting the workspace and transferring
#:           ownership
#:   admin   manage members and invitations, plus everything an editor can do
#:   editor  create and delete content: documents, reports, research, chat
#:   viewer  read only
PERMISSIONS: dict[str, frozenset[str]] = {
    "workspace.read": frozenset({"owner", "admin", "editor", "viewer"}),
    "workspace.update": frozenset({"owner", "admin"}),
    "workspace.delete": frozenset({"owner"}),
    "workspace.transfer": frozenset({"owner"}),
    "members.read": frozenset({"owner", "admin", "editor", "viewer"}),
    "members.invite": frozenset({"owner", "admin"}),
    "members.remove": frozenset({"owner", "admin"}),
    "members.update_role": frozenset({"owner", "admin"}),
    "content.read": frozenset({"owner", "admin", "editor", "viewer"}),
    "content.write": frozenset({"owner", "admin", "editor"}),
    "content.delete": frozenset({"owner", "admin", "editor"}),
}

#: Where the resolved role is stashed for the life of the request, so a route
#: that already resolved the workspace does not pay for a second lookup.
_ROLE_ATTR = "workspace_role"


def membership_for(db: Session, workspace_id: str, user_id: str) -> WorkspaceMember | None:
    """The caller's membership row, or None if they are not a member."""
    return (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
        .first()
    )


def role_in_workspace(db: Session, workspace_id: str, user_id: str) -> str | None:
    """The caller's role in a workspace, or None if they are not a member."""
    membership = membership_for(db, workspace_id, user_id)
    return membership.role if membership else None


def record_role(request: Request, workspace_id: str, role: str | None) -> None:
    """Remember the resolved role for the rest of this request."""
    if request is not None and hasattr(request, "state"):
        setattr(request.state, _ROLE_ATTR, {"workspace_id": workspace_id, "role": role})


def current_role(request: Request, workspace_id: str) -> str | None:
    """The role recorded by resolve_workspace earlier in this request.

    Returns None when the workspace was never resolved, or a different one
    was, so a caller cannot be granted a role it was not checked for.
    """
    if request is None or not hasattr(request, "state"):
        return None
    recorded = getattr(request.state, _ROLE_ATTR, None)
    if not recorded or recorded.get("workspace_id") != workspace_id:
        return None
    return recorded.get("role")


def can(role: str | None, permission: str) -> bool:
    """Whether ``role`` grants ``permission``.

    An unknown permission grants nothing. Failing closed matters here: a
    typo in a permission name must not silently authorise everyone.
    """
    allowed = PERMISSIONS.get(permission)
    if allowed is None:
        return False
    return (role or "").strip().lower() in allowed


def create_owner_membership(db: Session, workspace, user, *, commit: bool = True):
    """Record ``user`` as the owner of a freshly created workspace.

    Every workspace needs an owner member row from the moment it exists:
    authorization reads workspace_members, so a workspace created without
    one is a workspace nobody -- including its creator -- can open.

    ``workspaces.owner_id`` is set as well and kept in step with this row.
    """
    from app.models.membership import WorkspaceMember

    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=WorkspaceRole.OWNER,
        # No inviter: an owner is not invited, they created the workspace.
        invited_by=None,
    )
    db.add(membership)
    if commit:
        db.commit()
    return membership


def member_count(db: Session, workspace_id: str) -> int:
    """How many people belong to a workspace."""
    from app.models.membership import WorkspaceMember

    return db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == workspace_id).count()


def require_workspace_role(
    request: Request,
    workspace,
    permission: str,
    current_user,
    *,
    db: Session | None = None,
) -> str:
    """Authorise ``permission`` against the caller's role. Returns the role.

    Called after ``resolve_workspace`` has already established membership,
    so this only answers "is that membership enough for this action". A
    member who is not permitted gets 403, not 404: they legitimately know
    the workspace exists, and pretending otherwise would be confusing rather
    than protective.
    """
    workspace_id = getattr(workspace, "id", None) or str(workspace)
    role = current_role(request, workspace_id)
    if role is None and db is not None:
        # A route that resolved the workspace some other way, or a caller
        # whose role was not recorded. Look it up rather than assume.
        role = role_in_workspace(db, workspace_id, current_user.id)
        record_role(request, workspace_id, role)

    if can(role, permission):
        return role

    metrics.safe(metrics.authz_denials_total.labels(reason="workspace_role").inc)
    audit(
        action=AuditAction.PERMISSION_DENIED,
        actor=current_user,
        outcome=AuditOutcome.DENIED,
        resource=f"workspace:{workspace_id}",
        request=request,
        metadata={
            "reason": "insufficient_workspace_role",
            "permission": permission,
            "role": role,
        },
    )
    raise HTTPException(
        status_code=403,
        detail="Your role in this workspace does not permit this action.",
    )
