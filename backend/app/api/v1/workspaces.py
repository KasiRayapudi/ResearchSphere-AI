from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.workspace import Workspace
from app.models.document import Document
from typing import List

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

@router.get("", response_model=List[WorkspaceResponse])
async def get_workspaces(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    workspaces = db.query(Workspace).filter(Workspace.owner_id == current_user.id).all()
    
    # Map to schema with counts
    result = []
    for ws in workspaces:
        doc_count = db.query(Document).filter(Document.workspace_id == ws.id).count()
        result.append(
            WorkspaceResponse(
                id=ws.id,
                name=ws.name,
                description=ws.description,
                memberCount=1,  # Default member count for MVP
                documentCount=doc_count,
                role="owner",
                createdAt=ws.created_at.strftime("%Y-%m-%d")
            )
        )
    return result

@router.post("", response_model=WorkspaceResponse)
async def create_workspace(
    payload: WorkspaceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    new_ws = Workspace(
        name=payload.name,
        description=payload.description,
        owner_id=current_user.id
    )
    db.add(new_ws)
    db.commit()
    db.refresh(new_ws)
    
    return WorkspaceResponse(
        id=new_ws.id,
        name=new_ws.name,
        description=new_ws.description,
        memberCount=1,
        documentCount=0,
        role="owner",
        createdAt=new_ws.created_at.strftime("%Y-%m-%d")
    )
