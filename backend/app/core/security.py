import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.audit import AuditAction, AuditOutcome, audit
from app.core.config import settings
from app.core.database import get_db


class BearerAuth(HTTPBearer):
    """HTTPBearer that returns 401 for missing/!malformed credentials.

    Starlette's default raises 403 when the Authorization header is absent,
    which conflates "not authenticated" with "not permitted". 403 is reserved
    here for an authenticated caller who lacks the required role.
    """

    def __init__(self):
        super().__init__(auto_error=False)

    async def __call__(self, request: Request):
        credentials = await super().__call__(request)
        if credentials is None or not credentials.credentials:
            audit(
                action=AuditAction.UNAUTHORIZED_ACCESS,
                outcome=AuditOutcome.DENIED,
                resource="auth:missing_credentials",
                request=request,
                metadata={"reason": "missing_or_malformed_authorization_header"},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credentials


security = BearerAuth()


def hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    return hashed.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


def _base_claims(token_type: str, expires_delta: timedelta) -> dict:
    """Standard registered claims shared by every token we issue."""
    now = datetime.now(UTC)
    return {
        "iat": now,  # issued at
        "nbf": now,  # not valid before
        "exp": now + expires_delta,  # expiry
        "jti": str(uuid.uuid4()),  # unique id, enables revocation
        "typ": token_type,  # access vs refresh - not interchangeable
        "iss": settings.JWT_ISSUER,  # issuer
        "aud": settings.JWT_AUDIENCE,  # audience
    }


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    to_encode.update(
        _base_claims(
            TOKEN_TYPE_ACCESS,
            expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
    )
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    to_encode.update(
        _base_claims(
            TOKEN_TYPE_REFRESH,
            expires_delta or timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        )
    )
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(
    token: str,
    expected_type: str = TOKEN_TYPE_ACCESS,
    check_revocation: bool = True,
) -> dict:
    """Decode and fully validate a token.

    Validates signature, expiry, not-before, issuer and audience, allows a
    small clock skew, and enforces the token type so a refresh token can never
    be presented as an access token.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={
                "require_exp": True,
                "require_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                # Tolerate small clock differences between issuer and verifier.
                "leeway": settings.JWT_CLOCK_SKEW_SECONDS,
            },
        )
    except JWTError as exc:
        # The token itself is never logged - only the reason it was rejected.
        audit(
            action=AuditAction.INVALID_TOKEN,
            outcome=AuditOutcome.DENIED,
            resource="auth:token",
            metadata={"reason": type(exc).__name__},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Token type must match what the caller expects.
    if expected_type and payload.get("typ") != expected_type:
        audit(
            action=AuditAction.INVALID_TOKEN,
            outcome=AuditOutcome.DENIED,
            resource="auth:token",
            metadata={
                "reason": "wrong_token_type",
                "expected": expected_type,
                "actual": payload.get("typ"),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Revocation check (Redis-backed; degrades open when Redis is down).
    #
    # Callers that own a stronger source of truth skip this. Refresh rotation
    # must: a consumed refresh token is blacklisted, and rejecting it here
    # would short-circuit reuse detection before the database can revoke the
    # token family - defeating theft detection exactly when Redis is up.
    jti = payload.get("jti")
    if check_revocation and jti:
        from app.core.token_store import is_token_revoked

        if is_token_revoked(jti):
            audit(
                action=AuditAction.INVALID_TOKEN,
                outcome=AuditOutcome.DENIED,
                resource="auth:token",
                metadata={"reason": "revoked", "jti": jti},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return payload


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    from app.models.user import User

    payload = decode_token(credentials.credentials)
    user_id: str = payload.get("sub")
    if not user_id:
        audit(
            action=AuditAction.INVALID_TOKEN,
            outcome=AuditOutcome.DENIED,
            resource="auth:token",
            request=request,
            metadata={"reason": "missing_subject_claim"},
        )
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        audit(
            action=AuditAction.UNAUTHORIZED_ACCESS,
            outcome=AuditOutcome.DENIED,
            resource=f"user:{user_id}",
            request=request,
            metadata={"reason": "user_not_found"},
        )
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        audit(
            action=AuditAction.UNAUTHORIZED_ACCESS,
            actor=user,
            outcome=AuditOutcome.DENIED,
            resource=f"user:{user.id}",
            request=request,
            metadata={"reason": "account_deactivated"},
        )
        raise HTTPException(status_code=401, detail="Account is deactivated")
    return user


# ---------------------------------------------------------------------------
# Role-based access control
# ---------------------------------------------------------------------------
#: Role hierarchy. A role satisfies a requirement when its rank is >= the
#: required rank, so "admin" implicitly satisfies "member".
ROLE_RANKS = {"viewer": 10, "member": 20, "researcher": 20, "admin": 90, "owner": 100}


def role_rank(role: str) -> int:
    return ROLE_RANKS.get((role or "").strip().lower(), 0)


def require_role(*roles: str):
    """Dependency factory enforcing that the caller holds one of ``roles``.

    Returns 403 (not 404 or 401) when an authenticated user lacks the role -
    the caller is known, they are simply not permitted. Every denial is
    audited.
    """
    required = [r.lower() for r in roles]
    minimum = min((role_rank(r) for r in required), default=0)

    def dependency(
        request: Request,
        current_user=Depends(get_current_user),
    ):
        user_role = (getattr(current_user, "role", "") or "").lower()
        if user_role in required or role_rank(user_role) >= minimum:
            return current_user
        metrics.safe(metrics.authz_denials_total.labels(reason="missing_role").inc)
        audit(
            action=AuditAction.PERMISSION_DENIED,
            actor=current_user,
            outcome=AuditOutcome.DENIED,
            resource=f"role:{'|'.join(required)}",
            request=request,
            metadata={"required_roles": required, "actual_role": user_role},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to perform this action.",
        )

    return dependency


def require_admin(
    request: Request,
    current_user=Depends(get_current_user),
):
    """Dependency enforcing administrator access."""
    user_role = (getattr(current_user, "role", "") or "").lower()
    if role_rank(user_role) >= ROLE_RANKS["admin"]:
        return current_user
    metrics.safe(metrics.authz_denials_total.labels(reason="not_admin").inc)
    audit(
        action=AuditAction.PERMISSION_DENIED,
        actor=current_user,
        outcome=AuditOutcome.DENIED,
        resource="role:admin",
        request=request,
        metadata={"required_roles": ["admin"], "actual_role": user_role},
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Administrator privileges are required.",
    )


def require_workspace_owner(
    workspace_id: str,
    request: Request,
    db: Session,
    current_user,
):
    """Return the workspace only when ``current_user`` is a member of it.

    This is the single choke point for workspace authorization. Callers must
    never query a workspace-scoped resource using a client-supplied
    ``workspace_id`` without going through here.

    Membership alone is checked. A route that needs more than "is a member"
    calls ``require_workspace_role`` afterwards, which reads the role this
    function recorded on the request.

    A workspace that does not exist and one the caller is not a member of
    both return 404, so the endpoint cannot be used to probe which workspace
    ids exist.
    """
    from app.core.workspace_access import record_role, role_in_workspace
    from app.models.membership import WorkspaceRole
    from app.models.workspace import Workspace

    workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()

    role = None
    if workspace is not None:
        # Read from the database on every request, never from the token: a
        # membership revoked a moment ago must not survive in a JWT that is
        # valid for another hour.
        role = role_in_workspace(db, workspace_id, current_user.id)

    if workspace is None or role is None:
        # A platform admin may reach any workspace that exists, for support
        # and moderation. That is the site-wide User.role, which is separate
        # from workspace roles and is not implied by either.
        if (
            workspace is not None
            and role_rank((getattr(current_user, "role", "") or "").lower()) >= ROLE_RANKS["admin"]
        ):
            record_role(request, workspace_id, WorkspaceRole.ADMIN)
            return workspace

        metrics.safe(metrics.authz_denials_total.labels(reason="workspace_isolation").inc)
        audit(
            action=AuditAction.PERMISSION_DENIED,
            actor=current_user,
            outcome=AuditOutcome.DENIED,
            resource=f"workspace:{workspace_id}",
            request=request,
            metadata={
                "reason": "not_found" if workspace is None else "not_a_member",
            },
        )
        raise HTTPException(status_code=404, detail="Workspace not found")

    record_role(request, workspace_id, role)
    return workspace


def resolve_workspace(
    workspace_id: str | None,
    request: Request,
    db: Session,
    current_user,
):
    """Resolve the workspace for a request, enforcing membership.

    When ``workspace_id`` is supplied it is verified against the caller's
    membership. When omitted, the caller's first workspace is used --
    preserving the existing convenience behaviour without ever trusting a
    client-supplied id.

    "First workspace" now means the earliest one they are a member of,
    which for a user who has only ever had their own is the same workspace
    as before.

    Returns ``None`` when the caller belongs to no workspace at all, so
    endpoints can keep returning an empty list instead of an error.
    """
    from app.core.workspace_access import record_role
    from app.models.membership import WorkspaceMember
    from app.models.workspace import Workspace

    if workspace_id:
        return require_workspace_owner(workspace_id, request, db, current_user)

    membership = (
        db.query(WorkspaceMember)
        .join(Workspace, WorkspaceMember.workspace_id == Workspace.id)
        .filter(WorkspaceMember.user_id == current_user.id)
        .order_by(Workspace.created_at.asc())
        .first()
    )
    if membership is None:
        return None

    workspace = db.query(Workspace).filter(Workspace.id == membership.workspace_id).first()
    if workspace is not None:
        record_role(request, workspace.id, membership.role)
    return workspace
