from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.audit import AuditAction, AuditOutcome, audit
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import ValidationException
from app.core.logging import get_logger
from app.core.password_policy import PasswordPolicyError, evaluate_password, validate_password
from app.core.password_reset import consume_reset_token, generate_reset_token, verify_reset_token
from app.core.refresh_service import issue_refresh_token, revoke_all_for_user, rotate_refresh_token
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    create_access_token,
    decode_token,
    get_current_user,
    hash_password,
    security,
    verify_password,
)
from app.core.token_store import revoke_token
from app.core.workspace_access import create_owner_membership
from app.models.user import User
from app.models.workspace import Workspace

router = APIRouter()
logger = get_logger("auth")


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
        metrics.safe(metrics.auth_attempts_total.labels(action="login", outcome="failure").inc)
        metrics.safe(
            metrics.auth_failures_total.labels(
                action="login", reason="bad_password" if user else "unknown_email"
            ).inc
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        )

    token = create_access_token({"sub": user.id, "role": user.role})
    refresh_token, _ = issue_refresh_token(
        db,
        user,
        user_agent=request.headers.get("User-Agent"),
        client_ip=request.client.host if request.client else None,
    )
    metrics.safe(metrics.auth_attempts_total.labels(action="login", outcome="success").inc)
    audit(
        action=AuditAction.LOGIN_SUCCESS,
        actor=user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"user:{user.id}",
        request=request,
    )
    return {
        "access_token": token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "name": user.full_name,
            "email": user.email,
            "role": user.role,
            "avatarUrl": user.avatar_url
            or "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=250&q=80",
        },
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
            status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered"
        )

    # Enforce the password policy before anything is persisted.
    try:
        validate_password(payload.password, email=payload.email, full_name=payload.name)
    except PasswordPolicyError as exc:
        audit(
            action=AuditAction.USER_REGISTERED,
            actor=payload.email,
            outcome=AuditOutcome.FAILURE,
            resource="user:new",
            request=request,
            metadata={"reason": "weak_password", "violations": exc.violations},
        )
        raise ValidationException(
            message="Password does not meet the security policy.",
            details=[
                {"loc": ["body", "password"], "msg": v, "type": "password_policy"}
                for v in exc.violations
            ],
        ) from exc

    # Create new user
    new_user = User(
        full_name=payload.name,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        role="member",
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # Automatically create a default workspace for the new user
    default_ws = Workspace(
        name=f"{payload.name}'s Workspace",
        description="Default workspace created automatically on sign up.",
        owner_id=new_user.id,
    )
    db.add(default_ws)
    db.flush()  # assigns default_ws.id without ending the transaction

    # The owner membership is created with the workspace: authorization reads
    # workspace_members, so a workspace without one cannot be opened by
    # anybody, including the account that was just registered.
    create_owner_membership(db, default_ws, new_user, commit=False)
    db.commit()

    metrics.safe(metrics.auth_attempts_total.labels(action="signup", outcome="success").inc)
    audit(
        action=AuditAction.USER_REGISTERED,
        actor=new_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"user:{new_user.id}",
        request=request,
        metadata={"default_workspace_id": default_ws.id},
    )

    token = create_access_token({"sub": new_user.id, "role": new_user.role})
    refresh_token, _ = issue_refresh_token(
        db,
        new_user,
        user_agent=request.headers.get("User-Agent"),
        client_ip=request.client.host if request.client else None,
    )
    return {
        "access_token": token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": new_user.id,
            "name": new_user.full_name,
            "email": new_user.email,
            "role": new_user.role,
            "avatarUrl": new_user.avatar_url
            or "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=250&q=80",
        },
    }


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "name": current_user.full_name,
        "email": current_user.email,
        "role": current_user.role,
        "avatarUrl": current_user.avatar_url
        or "https://images.unsplash.com/photo-1534528741775-53994a69daeb?auto=format&fit=crop&w=250&q=80",
    }


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str


class PasswordStrengthRequest(BaseModel):
    password: str
    email: str | None = None
    name: str | None = None


@router.post("/password-reset/request")
async def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Begin a password reset.

    Always returns the same response whether or not the address exists, so the
    endpoint cannot be used to enumerate registered accounts.
    """
    generic_response = {
        "message": "If an account exists for that address, a reset link has been sent.",
    }

    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not user.is_active:
        audit(
            action=AuditAction.CREDENTIAL_RESET_REQUESTED,
            actor=payload.email,
            outcome=AuditOutcome.FAILURE,
            resource="user:unknown",
            request=request,
            metadata={"reason": "unknown_or_inactive_account"},
        )
        return generic_response

    client_ip = request.client.host if request.client else None
    raw_token, record = generate_reset_token(db, user.id, requested_ip=client_ip)

    audit(
        action=AuditAction.CREDENTIAL_RESET_REQUESTED,
        actor=user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"user:{user.id}",
        request=request,
        metadata={"token_id": record.id, "expires_at": record.expires_at.isoformat()},
    )

    # No email transport is configured yet. The raw token is deliberately NOT
    # returned in production: doing so would let anyone who can call this
    # endpoint reset any account. Outside production it is returned so the
    # flow is testable end to end.
    if not settings.is_production:
        return {**generic_response, "debug_reset_token": raw_token}
    logger.warning(
        "Password reset requested but no email transport is configured; "
        "the token was generated and discarded."
    )
    return generic_response


@router.post("/password-reset/confirm")
async def confirm_password_reset(
    payload: PasswordResetConfirm,
    request: Request,
    db: Session = Depends(get_db),
):
    """Complete a password reset using a single-use token."""
    record = verify_reset_token(db, payload.token)
    if record is None:
        # Unknown, expired, used and revoked tokens are indistinguishable here
        # on purpose.
        audit(
            action=AuditAction.CREDENTIAL_RESET_COMPLETED,
            outcome=AuditOutcome.DENIED,
            resource="password_reset:token",
            request=request,
            metadata={"reason": "invalid_or_expired_token"},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has expired.",
        )

    user = db.query(User).filter(User.id == record.user_id).first()
    if not user or not user.is_active:
        audit(
            action=AuditAction.CREDENTIAL_RESET_COMPLETED,
            outcome=AuditOutcome.DENIED,
            resource=f"user:{record.user_id}",
            request=request,
            metadata={"reason": "unknown_or_inactive_account"},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This reset link is invalid or has expired.",
        )

    try:
        validate_password(payload.new_password, email=user.email, full_name=user.full_name)
    except PasswordPolicyError as exc:
        audit(
            action=AuditAction.CREDENTIAL_RESET_COMPLETED,
            actor=user,
            outcome=AuditOutcome.FAILURE,
            resource=f"user:{user.id}",
            request=request,
            metadata={"reason": "weak_password", "violations": exc.violations},
        )
        raise ValidationException(
            message="Password does not meet the security policy.",
            details=[
                {"loc": ["body", "new_password"], "msg": v, "type": "password_policy"}
                for v in exc.violations
            ],
        ) from exc

    if verify_password(payload.new_password, user.hashed_password):
        audit(
            action=AuditAction.CREDENTIAL_RESET_COMPLETED,
            actor=user,
            outcome=AuditOutcome.FAILURE,
            resource=f"user:{user.id}",
            request=request,
            metadata={"reason": "password_reuse"},
        )
        raise ValidationException(
            message="Password does not meet the security policy.",
            details=[
                {
                    "loc": ["body", "new_password"],
                    "msg": "New password must differ from the current password.",
                    "type": "password_reuse",
                }
            ],
        )

    user.hashed_password = hash_password(payload.new_password)
    # Single-use: mark consumed and revoke every other outstanding token.
    consume_reset_token(db, record)
    db.commit()

    audit(
        action=AuditAction.CREDENTIAL_RESET_COMPLETED,
        actor=user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"user:{user.id}",
        request=request,
        metadata={"token_id": record.id},
    )
    return {"message": "Your password has been reset. Please sign in again."}


@router.post("/password/strength")
async def check_password_strength(payload: PasswordStrengthRequest):
    """Score a password against the policy without creating anything.

    Lets the UI give live feedback. The password is never stored or logged.
    """
    result = evaluate_password(payload.password, email=payload.email, full_name=payload.name)
    return {
        "score": result.score,
        "label": result.label,
        "entropyBits": result.entropy_bits,
        "valid": result.is_valid,
        "violations": result.violations,
    }


# ---------------------------------------------------------------------------
# Refresh tokens and session management
# ---------------------------------------------------------------------------
class RefreshRequest(BaseModel):
    refresh_token: str


@router.post("/refresh")
async def refresh_access_token(
    payload: RefreshRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Exchange a refresh token for a new access + refresh token pair.

    The presented token is consumed (rotation). Presenting one that has
    already been rotated is treated as theft: the whole token family is
    revoked, ending the session for attacker and user alike.
    """
    result = rotate_refresh_token(
        db,
        payload.refresh_token,
        user_agent=request.headers.get("User-Agent"),
        client_ip=request.client.host if request.client else None,
    )

    if result.reuse_detected:
        metrics.safe(metrics.token_refresh_reuse_total.inc)
        metrics.safe(
            metrics.auth_attempts_total.labels(action="refresh", outcome="reuse_detected").inc
        )
        audit(
            action=AuditAction.TOKEN_REUSE_DETECTED,
            outcome=AuditOutcome.DENIED,
            resource="auth:refresh_token",
            request=request,
            metadata={
                "reason": result.reason,
                "revoked_tokens": result.revoked_count,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This session has been terminated. Please sign in again.",
        )

    if not result.ok:
        audit(
            action=AuditAction.TOKEN_REFRESHED,
            outcome=AuditOutcome.DENIED,
            resource="auth:refresh_token",
            request=request,
            metadata={"reason": result.reason},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
        )

    metrics.safe(metrics.auth_attempts_total.labels(action="refresh", outcome="success").inc)
    audit(
        action=AuditAction.TOKEN_REFRESHED,
        actor=result.user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"user:{result.user.id}",
        request=request,
    )
    return {
        "access_token": result.access_token,
        "refresh_token": result.refresh_token,
        "token_type": "bearer",
    }


@router.post("/logout")
async def logout(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Log out: blacklist the current access token and revoke all refresh
    tokens for the user, invalidating every session."""
    payload = decode_token(credentials.credentials, expected_type=TOKEN_TYPE_ACCESS)
    blacklisted = revoke_token(payload.get("jti"), payload.get("exp"))
    revoked = revoke_all_for_user(db, current_user.id, reason="logout")

    metrics.safe(metrics.auth_attempts_total.labels(action="logout", outcome="success").inc)
    audit(
        action=AuditAction.LOGOUT,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"user:{current_user.id}",
        request=request,
        metadata={
            "refresh_tokens_revoked": revoked,
            # False means Redis was unavailable, so the access token stays
            # valid until it expires. Refresh tokens are revoked regardless.
            "access_token_blacklisted": blacklisted,
        },
    )
    return {"message": "Signed out successfully.", "sessionsRevoked": revoked}
