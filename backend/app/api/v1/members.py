"""
Workspace members and invitations.

Everything here goes through the same two checks as the rest of the API:
``resolve_workspace`` establishes membership, ``require_workspace_role``
establishes that the membership is enough. No role logic is written twice.

Acceptance is the one exception: someone accepting an invitation is by
definition not yet a member, so that endpoint authenticates the user and
then authorises on the invitation token instead.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.audit import AuditAction, AuditOutcome, audit
from app.core.database import get_db
from app.core.invitations import (
    find_by_token,
    generate_invitation,
    invitation_message,
    normalise_email,
)
from app.core.logging import get_logger
from app.core.pagination import PageParams, page_params, paginate
from app.core.security import get_current_user
from app.core.workspace_access import membership_for, require_workspace_role
from app.models.membership import WorkspaceInvitation, WorkspaceMember, WorkspaceRole
from app.models.user import User
from app.models.workspace import Workspace
from app.realtime import notify
from app.services.email import send_email

router = APIRouter()
logger = get_logger("members")


# --------------------------------------------------------------- schemas --
class MemberResponse(BaseModel):
    id: str
    userId: str
    email: str
    fullName: str | None
    role: str
    joinedAt: str
    invitedBy: str | None


class InvitationResponse(BaseModel):
    id: str
    email: str
    role: str
    expiresAt: str
    createdAt: str
    invitedBy: str | None
    status: str


class InviteRequest(BaseModel):
    email: EmailStr
    role: str = WorkspaceRole.VIEWER
    workspace_id: str | None = None


class AcceptRequest(BaseModel):
    token: str


class RevokeRequest(BaseModel):
    invitation_id: str
    workspace_id: str | None = None


class ResendRequest(BaseModel):
    invitation_id: str
    workspace_id: str | None = None


class RoleUpdateRequest(BaseModel):
    role: str
    workspace_id: str | None = None


# --------------------------------------------------------------- helpers --
def _assignable_role(role: str) -> str:
    """Validate a role that is about to be granted.

    Owner is excluded: there is exactly one, and it moves only through the
    transfer endpoint, which also demotes the previous holder. Allowing it
    here would violate the single-owner index and fail confusingly.
    """
    value = (role or "").strip().lower()
    if value == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=400,
            detail="Ownership is transferred, not assigned. Use the transfer endpoint.",
        )
    if not WorkspaceRole.is_valid(value):
        raise HTTPException(
            status_code=400,
            detail="Unknown role. Choose one of: admin, editor, viewer.",
        )
    return value


def _resolve(workspace_id: str | None, request: Request, db: Session, current_user: User):
    """The workspace this request is about, or 400 if there is none."""
    from app.core.security import resolve_workspace

    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        raise HTTPException(status_code=400, detail="Workspace required")
    return ws


def _users_by_id(db: Session, user_ids) -> dict[str, User]:
    """Fetch several users in one query.

    Rendering a member list used to issue two queries per row -- one for the
    member, one for whoever invited them -- so a workspace of fifty people
    cost a hundred round trips. One IN query answers all of them.
    """
    wanted = {uid for uid in user_ids if uid}
    if not wanted:
        return {}
    return {user.id: user for user in db.query(User).filter(User.id.in_(wanted)).all()}


def _member_payload(member: WorkspaceMember, users: dict[str, User]) -> MemberResponse:
    """One member row, using a prefetched user map."""
    user = users.get(member.user_id)
    inviter = users.get(member.invited_by) if member.invited_by else None
    return MemberResponse(
        id=member.id,
        userId=member.user_id,
        email=user.email if user else "(removed account)",
        fullName=user.full_name if user else None,
        role=member.role,
        joinedAt=member.joined_at.isoformat() if member.joined_at else "",
        invitedBy=inviter.full_name if inviter else None,
    )


def _one_member(db: Session, member: WorkspaceMember) -> MemberResponse:
    """A single member row, for endpoints that return exactly one."""
    return _member_payload(member, _users_by_id(db, [member.user_id, member.invited_by]))


#: Sortable columns for the member list.
MEMBER_SORTS = {
    "joinedAt": WorkspaceMember.joined_at,
    "role": WorkspaceMember.role,
}

INVITATION_SORTS = {
    "createdAt": WorkspaceInvitation.created_at,
    "expiresAt": WorkspaceInvitation.expires_at,
    "email": WorkspaceInvitation.email,
    "role": WorkspaceInvitation.role,
}


def _invitation_status(invitation: WorkspaceInvitation) -> str:
    if invitation.accepted_at:
        return "accepted"
    if invitation.revoked_at:
        return "revoked"
    if invitation.expires_at <= datetime.utcnow():
        return "expired"
    return "pending"


# --------------------------------------------------------------- members --
@router.get("/members")
async def list_members(
    request: Request,
    workspace_id: str | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Everyone in the workspace. Any member may see the list."""
    ws = _resolve(workspace_id, request, db, current_user)
    require_workspace_role(request, ws, "members.read", current_user, db=db)

    query = db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws.id)
    page = paginate(
        query,
        params,
        sortable=MEMBER_SORTS,
        default_sort="joinedAt",
        tiebreaker=WorkspaceMember.id,
    )

    # One query for every user on the page, rather than two per row.
    users = _users_by_id(db, [m.user_id for m in page.items] + [m.invited_by for m in page.items])
    return page.envelope([_member_payload(member, users) for member in page.items])


@router.patch("/members/{member_id}", response_model=MemberResponse)
async def update_member_role(
    member_id: str,
    payload: RoleUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Change a member's role."""
    ws = _resolve(payload.workspace_id, request, db, current_user)
    actor_role = require_workspace_role(request, ws, "members.update_role", current_user, db=db)
    new_role = _assignable_role(payload.role)

    member = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.id == member_id, WorkspaceMember.workspace_id == ws.id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    if member.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=400,
            detail="The owner's role cannot be changed. Transfer ownership instead.",
        )

    # An admin must not be able to demote another admin: that is a lateral
    # move against a peer, and it lets one admin quietly take over. Only the
    # owner outranks an admin.
    if member.role == WorkspaceRole.ADMIN and actor_role != WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=403,
            detail="Only the workspace owner can change another admin's role.",
        )

    previous = member.role
    member.role = new_role
    db.commit()
    db.refresh(member)
    await notify.member_role_changed(ws.id, member, actor_id=current_user.id)

    audit(
        action=AuditAction.WORKSPACE_MEMBER_ROLE_CHANGED,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"workspace:{ws.id}",
        request=request,
        metadata={
            "member_id": member.id,
            "target_user_id": member.user_id,
            "from_role": previous,
            "to_role": new_role,
        },
    )
    return _one_member(db, member)


@router.delete("/members/{member_id}")
async def remove_member(
    member_id: str,
    request: Request,
    workspace_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove a member from the workspace.

    A member may always remove themselves; removing anyone else needs the
    members.remove permission.
    """
    ws = _resolve(workspace_id, request, db, current_user)

    member = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.id == member_id, WorkspaceMember.workspace_id == ws.id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")

    leaving = member.user_id == current_user.id
    if not leaving:
        actor_role = require_workspace_role(request, ws, "members.remove", current_user, db=db)
        # Same reasoning as demotion: an admin cannot remove a peer.
        if member.role == WorkspaceRole.ADMIN and actor_role != WorkspaceRole.OWNER:
            raise HTTPException(
                status_code=403,
                detail="Only the workspace owner can remove another admin.",
            )

    if member.role == WorkspaceRole.OWNER:
        # Removing the owner would leave the workspace with nobody able to
        # administer it, and would break the owner_id pointer.
        raise HTTPException(
            status_code=400,
            detail=(
                "The owner cannot be removed. Transfer ownership first, or delete " "the workspace."
            ),
        )

    target_user_id = member.user_id
    db.delete(member)
    db.commit()

    audit(
        action=AuditAction.WORKSPACE_MEMBER_REMOVED,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"workspace:{ws.id}",
        request=request,
        metadata={"target_user_id": target_user_id, "self_removal": leaving},
    )
    await notify.member_removed(ws.id, member_id, target_user_id, actor_id=current_user.id)
    return {"message": "Member removed"}


@router.post("/transfer-ownership", response_model=list[MemberResponse])
async def transfer_ownership(
    payload: RoleUpdateRequest,
    request: Request,
    member_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Hand ownership to another member.

    The previous owner becomes an admin rather than losing access, and
    workspaces.owner_id is moved in the same transaction so the denormalised
    pointer cannot drift from the membership table.
    """
    ws = _resolve(payload.workspace_id, request, db, current_user)
    require_workspace_role(request, ws, "workspace.transfer", current_user, db=db)

    target = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.id == member_id, WorkspaceMember.workspace_id == ws.id)
        .first()
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Member not found")
    if target.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You already own this workspace.")

    current_owner = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.role == WorkspaceRole.OWNER,
        )
        .first()
    )

    # Demote first: the single-owner index rejects two owners, so the order
    # matters. Both writes are in one transaction, so a failure leaves the
    # previous owner in place rather than a workspace with none.
    if current_owner is not None:
        current_owner.role = WorkspaceRole.ADMIN
        db.flush()
    target.role = WorkspaceRole.OWNER
    ws.owner_id = target.user_id
    db.commit()

    audit(
        action=AuditAction.WORKSPACE_OWNERSHIP_TRANSFERRED,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"workspace:{ws.id}",
        request=request,
        metadata={
            "from_user_id": current_owner.user_id if current_owner else None,
            "to_user_id": target.user_id,
        },
    )
    # Two roles changed. Announced separately because each is a change to a
    # different member's live sockets, and to the member list.
    if current_owner is not None:
        await notify.member_role_changed(ws.id, current_owner, actor_id=current_user.id)
    await notify.member_role_changed(ws.id, target, actor_id=current_user.id)

    members = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.workspace_id == ws.id)
        .order_by(WorkspaceMember.joined_at.asc())
        .all()
    )
    users = _users_by_id(db, [m.user_id for m in members] + [m.invited_by for m in members])
    return [_member_payload(member, users) for member in members]


# ----------------------------------------------------------- invitations --
@router.get("/invitations")
async def list_invitations(
    request: Request,
    workspace_id: str | None = None,
    status: str | None = Query(
        None, description="Filter by pending / accepted / revoked / expired."
    ),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Invitations for this workspace, most recent first."""
    ws = _resolve(workspace_id, request, db, current_user)
    require_workspace_role(request, ws, "members.invite", current_user, db=db)

    query = db.query(WorkspaceInvitation).filter(WorkspaceInvitation.workspace_id == ws.id)
    # Status is derived from accepted_at / revoked_at / expires_at rather
    # than stored, so it is filtered after the rows are built rather than in
    # SQL. That means a status filter narrows the page, not the query; the
    # totals still describe the unfiltered set, which is the honest reading
    # of "how many invitations does this workspace have".
    page = paginate(
        query,
        params,
        sortable=INVITATION_SORTS,
        default_sort="createdAt",
        tiebreaker=WorkspaceInvitation.id,
        searchable=[WorkspaceInvitation.email],
    )

    # Previously this ran the same user lookup twice for every row.
    inviters = _users_by_id(db, [inv.invited_by for inv in page.items])
    rows = [
        InvitationResponse(
            id=inv.id,
            email=inv.email,
            role=inv.role,
            expiresAt=inv.expires_at.isoformat(),
            createdAt=inv.created_at.isoformat() if inv.created_at else "",
            invitedBy=(
                inviters[inv.invited_by].full_name
                if inv.invited_by and inv.invited_by in inviters
                else None
            ),
            status=_invitation_status(inv),
        )
        for inv in page.items
    ]
    if status:
        rows = [row for row in rows if row.status == status]
    return page.envelope(rows)


@router.post("/invite", response_model=InvitationResponse)
async def invite_member(
    payload: InviteRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Invite an email address to the workspace."""
    ws = _resolve(payload.workspace_id, request, db, current_user)
    require_workspace_role(request, ws, "members.invite", current_user, db=db)
    role = _assignable_role(payload.role)
    email = normalise_email(payload.email)

    # Already a member: re-inviting would create an invitation that can never
    # be accepted, and hints at a stale members list in front of the user.
    existing_user = db.query(User).filter(User.email == email).first()
    if existing_user and membership_for(db, ws.id, existing_user.id):
        raise HTTPException(
            status_code=409, detail="That person is already a member of this workspace."
        )

    # One live invitation per address per workspace. A second would mean two
    # valid tokens, and revoking one would not revoke the other.
    live = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.workspace_id == ws.id,
            WorkspaceInvitation.email == email,
            WorkspaceInvitation.accepted_at.is_(None),
            WorkspaceInvitation.revoked_at.is_(None),
            WorkspaceInvitation.expires_at > datetime.utcnow(),
        )
        .first()
    )
    if live is not None:
        raise HTTPException(
            status_code=409,
            detail="There is already a pending invitation for that address. Resend it instead.",
        )

    try:
        token, invitation = generate_invitation(
            db,
            workspace_id=ws.id,
            email=email,
            role=role,
            invited_by=current_user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    subject, body = invitation_message(
        ws.name, current_user.full_name or "A colleague", role, token
    )
    delivered = send_email(email, subject, body)

    audit(
        action=AuditAction.WORKSPACE_INVITED,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"workspace:{ws.id}",
        request=request,
        metadata={
            "invitation_id": invitation.id,
            "email": email,
            "role": role,
            "delivered": delivered,
        },
    )
    if not delivered:
        logger.warning(
            f"Invitation {invitation.id} was created but not delivered: no email "
            "transport is configured. Set EMAIL_BACKEND=smtp to send invitations."
        )

    # Never carries the token: it is a credential issued to one address,
    # and this reaches every connected member of the workspace.
    await notify.invitation_sent(ws.id, invitation, actor_id=current_user.id)

    return InvitationResponse(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        expiresAt=invitation.expires_at.isoformat(),
        createdAt=invitation.created_at.isoformat() if invitation.created_at else "",
        invitedBy=current_user.full_name,
        status=_invitation_status(invitation),
    )


@router.post("/invite/accept")
async def accept_invitation(
    payload: AcceptRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept an invitation and join the workspace.

    The caller is authenticated but not yet a member, so authorization here
    rests on the token rather than on ``resolve_workspace``.
    """
    invitation = find_by_token(db, payload.token)

    # Every rejection returns the same shape, so a caller cannot use this to
    # learn whether a token exists.
    if invitation is None or not invitation.is_pending():
        reason = "unknown"
        if invitation is not None:
            reason = _invitation_status(invitation)
        audit(
            action=AuditAction.WORKSPACE_INVITE_ACCEPTED,
            actor=current_user,
            outcome=AuditOutcome.DENIED,
            resource="workspace_invitation",
            request=request,
            metadata={"reason": reason},
        )
        raise HTTPException(status_code=400, detail="This invitation is invalid or has expired.")

    # The invitation is addressed to an email, so it can only be used by the
    # person who owns that address. Otherwise a leaked token would let anyone
    # join.
    if normalise_email(current_user.email) != invitation.email:
        audit(
            action=AuditAction.WORKSPACE_INVITE_ACCEPTED,
            actor=current_user,
            outcome=AuditOutcome.DENIED,
            resource=f"workspace:{invitation.workspace_id}",
            request=request,
            metadata={"reason": "email_mismatch"},
        )
        raise HTTPException(
            status_code=403,
            detail="This invitation was sent to a different email address.",
        )

    workspace = db.query(Workspace).filter(Workspace.id == invitation.workspace_id).first()
    if workspace is None:
        raise HTTPException(status_code=404, detail="That workspace no longer exists.")

    existing = membership_for(db, invitation.workspace_id, current_user.id)
    # Kept so the new membership can be announced without reading it back.
    joined: WorkspaceMember | None = None
    if existing is None:
        joined = WorkspaceMember(
            workspace_id=invitation.workspace_id,
            user_id=current_user.id,
            role=invitation.role,
            invited_by=invitation.invited_by,
        )
        db.add(joined)
    invitation.accepted_at = datetime.utcnow()
    db.commit()

    audit(
        action=AuditAction.WORKSPACE_INVITE_ACCEPTED,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"workspace:{invitation.workspace_id}",
        request=request,
        metadata={
            "invitation_id": invitation.id,
            "role": invitation.role,
            "already_member": existing is not None,
        },
    )

    # Two things happened: an invitation was used, and (usually) a member
    # joined. The members list and the pending-invitations list are separate
    # screens, so both are told rather than one being inferred from the other.
    await notify.invitation_accepted(invitation.workspace_id, invitation.id, current_user.id)
    if joined is not None:
        await notify.member_added(
            invitation.workspace_id, joined, current_user, actor_id=current_user.id
        )
    return {
        "message": "Invitation accepted",
        "workspaceId": workspace.id,
        "workspaceName": workspace.name,
        "role": existing.role if existing else invitation.role,
    }


@router.post("/invite/revoke")
async def revoke_invitation(
    payload: RevokeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Withdraw a pending invitation."""
    ws = _resolve(payload.workspace_id, request, db, current_user)
    require_workspace_role(request, ws, "members.invite", current_user, db=db)

    invitation = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.id == payload.invitation_id,
            WorkspaceInvitation.workspace_id == ws.id,
        )
        .first()
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invitation.accepted_at is not None:
        raise HTTPException(
            status_code=400,
            detail="That invitation has already been accepted. Remove the member instead.",
        )

    invitation.revoked_at = datetime.utcnow()
    db.commit()

    audit(
        action=AuditAction.WORKSPACE_INVITE_REVOKED,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"workspace:{ws.id}",
        request=request,
        metadata={"invitation_id": invitation.id, "email": invitation.email},
    )
    await notify.invitation_revoked(ws.id, invitation.id, actor_id=current_user.id)
    return {"message": "Invitation revoked"}


@router.post("/invite/resend", response_model=InvitationResponse)
async def resend_invitation(
    payload: ResendRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a fresh token for an existing invitation.

    The old token is revoked and a new one issued rather than the same value
    being sent again: the previous email may have gone to the wrong place,
    and re-sending it would not fix that.
    """
    ws = _resolve(payload.workspace_id, request, db, current_user)
    require_workspace_role(request, ws, "members.invite", current_user, db=db)

    invitation = (
        db.query(WorkspaceInvitation)
        .filter(
            WorkspaceInvitation.id == payload.invitation_id,
            WorkspaceInvitation.workspace_id == ws.id,
        )
        .first()
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invitation.accepted_at is not None:
        raise HTTPException(status_code=400, detail="That invitation was already accepted.")

    invitation.revoked_at = datetime.utcnow()
    db.flush()

    token, replacement = generate_invitation(
        db,
        workspace_id=ws.id,
        email=invitation.email,
        role=invitation.role,
        invited_by=current_user.id,
    )
    subject, body = invitation_message(
        ws.name, current_user.full_name or "A colleague", replacement.role, token
    )
    delivered = send_email(replacement.email, subject, body)

    audit(
        action=AuditAction.WORKSPACE_INVITED,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"workspace:{ws.id}",
        request=request,
        metadata={
            "invitation_id": replacement.id,
            "replaces": invitation.id,
            "email": replacement.email,
            "delivered": delivered,
            "resend": True,
        },
    )
    # A resend is a revocation and a new invitation with a new id; a
    # manager's list must drop the one and show the other.
    await notify.invitation_revoked(ws.id, invitation.id, actor_id=current_user.id)
    await notify.invitation_sent(ws.id, replacement, actor_id=current_user.id)
    return InvitationResponse(
        id=replacement.id,
        email=replacement.email,
        role=replacement.role,
        expiresAt=replacement.expires_at.isoformat(),
        createdAt=replacement.created_at.isoformat() if replacement.created_at else "",
        invitedBy=current_user.full_name,
        status=_invitation_status(replacement),
    )
