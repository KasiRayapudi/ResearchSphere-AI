from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.database import get_db
from app.core.audit import audit, AuditAction, AuditOutcome

import bcrypt
security = HTTPBearer()


def hash_password(password: str) -> str:
    pwd_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    return hashed.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(
            plain_password.encode('utf-8'),
            hashed_password.encode('utf-8')
        )
    except Exception:
        return False


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
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
        )


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
