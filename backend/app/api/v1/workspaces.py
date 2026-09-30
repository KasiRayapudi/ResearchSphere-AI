from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import get_current_user, require_workspace_owner
from app.core.workspace_access import create_owner_membership, current_role, member_count
from app.models.document import Document
from app.models.membership import WorkspaceMember
from app.models.user import User
from app.models.workspace import Workspace

router = APIRouter()


class WorkspaceCreate(BaseModel):
    name: str
    description: str | None = None


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    description: str | None
    memberCount: int
    documentCount: int
    role: str
    createdAt: str

    class Config:
        from_attributes = True


@router.get("", response_model=list[WorkspaceResponse])
async def get_workspaces(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_user)
):
    # Every workspace the caller is a member of, not only the ones they
    # own. The role comes from their membership row, so a workspace someone
    # shared with them appears with the role they were actually given.
    memberships = (
        db.query(WorkspaceMember, Workspace)
        .join(Workspace, WorkspaceMember.workspace_id == Workspace.id)
        .filter(WorkspaceMember.user_id == current_user.id)
        .order_by(Workspace.created_at.asc())
        .all()
    )

    result = []
    for membership, ws in memberships:
        doc_count = db.query(Document).filter(Document.workspace_id == ws.id).count()
        result.append(
            WorkspaceResponse(
                id=ws.id,
                name=ws.name,
                description=ws.description,
                memberCount=member_count(db, ws.id),
                documentCount=doc_count,
                role=membership.role,
                createdAt=ws.created_at.strftime("%Y-%m-%d"),
            )
        )
    return result


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch a single workspace, enforcing membership."""
    ws = require_workspace_owner(workspace_id, request, db, current_user)
    doc_count = db.query(Document).filter(Document.workspace_id == ws.id).count()
    return WorkspaceResponse(
        id=ws.id,
        name=ws.name,
        description=ws.description,
        memberCount=member_count(db, ws.id),
        documentCount=doc_count,
        # Recorded by require_workspace_owner a moment ago.
        role=current_role(request, ws.id) or "viewer",
        createdAt=ws.created_at.strftime("%Y-%m-%d"),
    )


@router.post("", response_model=WorkspaceResponse)
async def create_workspace(
    payload: WorkspaceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    new_ws = Workspace(name=payload.name, description=payload.description, owner_id=current_user.id)
    db.add(new_ws)
    db.flush()  # assigns new_ws.id without ending the transaction

    # Committed together: a workspace without an owner member is one nobody
    # can open, including the person who just created it.
    create_owner_membership(db, new_ws, current_user, commit=False)
    db.commit()
    db.refresh(new_ws)

    return WorkspaceResponse(
        id=new_ws.id,
        name=new_ws.name,
        description=new_ws.description,
        memberCount=member_count(db, new_ws.id),
        documentCount=0,
        role="owner",
        createdAt=new_ws.created_at.strftime("%Y-%m-%d"),
    )
