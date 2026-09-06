from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import create_access_token, hash_password, verify_password, get_current_user
from app.models.user import User
from app.models.workspace import Workspace
from app.core.audit import audit, AuditAction, AuditOutcome

router = APIRouter()

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class UserSignup(BaseModel):
    name: str
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    role: str
    avatarUrl: str | None = None

    class Config:
        from_attributes = True

@router.post("/login")
async def login(payload: UserLogin, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        # The submitted password is never logged, only the outcome and which
        # of the two failure modes occurred.
        audit(
            action=AuditAction.LOGIN_FAILURE,
            actor=payload.email,
            outcome=AuditOutcome.FAILURE,
            resource=f"user:{user.id}" if user else "user:unknown",
            request=request,
            metadata={"reason": "bad_password" if user else "unknown_email"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password"
        )

    token = create_access_token({"sub": user.id, "role": user.role})
    audit(
        action=AuditAction.LOGIN_SUCCESS,
        actor=user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"user:{user.id}",
        request=request,
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "name": user.full_name,
            "email": user.email,
            "role": user.role,
            "avatarUrl": user.avatar_url or "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=250&q=80",
        }
    }

@router.post("/signup")
async def signup(payload: UserSignup, request: Request, db: Session = Depends(get_db)):
    # Check if user already exists
    existing_user = db.query(User).filter(User.email == payload.email).first()
    if existing_user:
        audit(
            action=AuditAction.USER_REGISTERED,
            actor=payload.email,
            outcome=AuditOutcome.FAILURE,
            resource="user:new",
            request=request,
            metadata={"reason": "email_already_registered"},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    # Create new user
    new_user = User(
        full_name=payload.name,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role="member"
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Automatically create a default workspace for the new user
    default_ws = Workspace(
        name=f"{payload.name}'s Workspace",
        description="Default workspace created automatically on sign up.",
        owner_id=new_user.id
    )
    db.add(default_ws)
    db.commit()

    audit(
        action=AuditAction.USER_REGISTERED,
        actor=new_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"user:{new_user.id}",
        request=request,
        metadata={"default_workspace_id": default_ws.id},
    )

    token = create_access_token({"sub": new_user.id, "role": new_user.role})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": new_user.id,
            "name": new_user.full_name,
            "email": new_user.email,
            "role": new_user.role,
            "avatarUrl": new_user.avatar_url or "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=250&q=80",
        }
    }

@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "name": current_user.full_name,
        "email": current_user.email,
        "role": current_user.role,
        "avatarUrl": current_user.avatar_url or "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=250&q=80",
    }
