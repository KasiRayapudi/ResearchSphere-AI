"""
Structured audit logging for ResearchSphere AI.

This module *extends* the Sprint 1 logging stack rather than replacing it:

- It reuses ``JSONFormatter`` so audit records share the application log format.
- It reuses ``request_id_var`` so audit records correlate with the request logs
  emitted by ``RequestIDMiddleware``.
- It writes to a dedicated ``researchsphere.audit`` logger with
  ``propagate=False`` and its own handler, so audit records stay separable from
  application logs and can later be routed to their own sink (file, SIEM,
  syslog) without touching application logging.

``setup_logging()`` only reconfigures the *root* logger, so configuring the
audit logger here is safe and survives the repeated ``setup_logging()`` calls
made at import time and again from the lifespan hook.

Usage::

    from app.core.audit import audit, AuditAction, AuditOutcome

    audit(
        action=AuditAction.LOGIN_SUCCESS,
        actor=user,
        resource="user:123",
        outcome=AuditOutcome.SUCCESS,
        request=request,
        metadata={"method": "password"},
    )
"""

import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from app.core.logging import JSONFormatter, request_id_var

# ---------------------------------------------------------------------------
# Dedicated audit logger
# ---------------------------------------------------------------------------
AUDIT_LOGGER_NAME = "researchsphere.audit"

_audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)


def _configure_audit_logger() -> logging.Logger:
    """Attach a JSON handler to the audit logger exactly once (idempotent)."""
    if not getattr(_audit_logger, "_researchsphere_configured", False):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        _audit_logger.addHandler(handler)
        _audit_logger.setLevel(logging.INFO)
        # Audit records must not be duplicated into the application log stream.
        _audit_logger.propagate = False
        _audit_logger._researchsphere_configured = True  # type: ignore[attr-defined]
    return _audit_logger


_configure_audit_logger()


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
class AuditAction(str, Enum):
    """Auditable actions. String-valued so they serialize directly to JSON."""

    # Authentication
    LOGIN_SUCCESS = "auth.login.success"
    LOGIN_FAILURE = "auth.login.failure"
    LOGOUT = "auth.logout"
    USER_REGISTERED = "auth.register"
    # Named for the event rather than the credential. The string values are
    # the stable contract that log queries, dashboards and alert rules match
    # on, and are deliberately unchanged.
    CREDENTIAL_RESET_REQUESTED = "auth.password_reset.requested"
    CREDENTIAL_RESET_COMPLETED = "auth.password_reset.completed"
    TOKEN_REFRESHED = "auth.token.refreshed"
    TOKEN_REUSE_DETECTED = "auth.token.reuse_detected"

    # Documents
    DOCUMENT_UPLOAD = "document.upload"
    DOCUMENT_DELETE = "document.delete"
    DOCUMENT_UPLOAD_REJECTED = "document.upload.rejected"
    DOCUMENT_DUPLICATE = "document.upload.duplicate"
    DOCUMENT_QUARANTINE = "document.quarantine"

    # Reports
    REPORT_GENERATE = "report.generate"

    # Connectors
    CONNECTOR_TOGGLE = "connector.toggle"

    # Admin
    ADMIN_ACCESS = "admin.access"
    ADMIN_ACTION = "admin.action"

    # Authorization / access control
    PERMISSION_DENIED = "authz.permission_denied"
    INVALID_TOKEN = "authz.invalid_token"
    UNAUTHORIZED_ACCESS = "authz.unauthorized_access"


class AuditOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------
REDACTED = "***REDACTED***"

#: Substrings that mark a key as sensitive. Matched case-insensitively against
#: the key name, so "hashed_password", "X-API-Key" and "refreshToken" all match.
SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "cookie",
    "session",
    "credential",
    "private_key",
    "gemini",
    "salt",
    "hash",
    "signature",
)

_MAX_DEPTH = 6
_MAX_VALUE_LEN = 1024
_MAX_ITEMS = 50


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _looks_like_token(value: str) -> bool:
    """Catch secrets that arrive under an innocuous key name.

    Covers JWTs (three dot-separated base64 segments) and Bearer headers.
    """
    stripped = value.strip()
    if stripped.lower().startswith("bearer "):
        return True
    parts = stripped.split(".")
    if len(parts) == 3 and all(parts) and len(stripped) > 60:
        # Header/payload/signature shaped and long enough to be a real JWT.
        return all(all(c.isalnum() or c in "-_=" for c in segment) for segment in parts)
    return False


def scrub_control_characters(value: str) -> str:
    """Strip characters that would let a value forge log structure.

    Newlines and carriage returns can split one record into several in a
    line-oriented log, letting a caller invent audit entries; other C0
    controls can corrupt a terminal reading it. Tabs are kept, being both
    printable in practice and common in legitimate content.
    """
    return "".join(ch for ch in value if ch.isprintable() or ch == "\t")


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact sensitive values from arbitrary metadata.

    Redacts by key name, by value shape (JWT/Bearer), and bounds depth, item
    count and string length so a caller cannot flood the audit log.
    """
    if _depth >= _MAX_DEPTH:
        return "<max-depth-exceeded>"

    if isinstance(value, Mapping):
        out = {}
        for idx, (k, v) in enumerate(value.items()):
            if idx >= _MAX_ITEMS:
                out["<truncated>"] = f"{len(value) - _MAX_ITEMS} more keys"
                break
            # A sensitive key name hides string values, but not numbers or
            # booleans: counts and flags like {"revoked_tokens": 3} or
            # {"access_token_blacklisted": True} are not secrets, and redacting
            # them would strip useful signal out of the audit trail.
            if _is_sensitive_key(k) and not isinstance(v, (int, float, bool)):
                out[str(k)] = REDACTED
            else:
                out[str(k)] = redact(v, _depth + 1)
        return out

    if isinstance(value, (list, tuple, set)):
        items = list(value)[:_MAX_ITEMS]
        return [redact(item, _depth + 1) for item in items]

    if isinstance(value, str):
        if _looks_like_token(value):
            return REDACTED
        value = scrub_control_characters(value)
        if len(value) > _MAX_VALUE_LEN:
            return value[:_MAX_VALUE_LEN] + "...<truncated>"
        return value

    if isinstance(value, (int, float, bool)) or value is None:
        return value

    # Unknown object: stringify defensively rather than leaking a repr with state.
    text = str(value)
    return text[:_MAX_VALUE_LEN]


# ---------------------------------------------------------------------------
# Actor / request extraction
# ---------------------------------------------------------------------------
def _clean(value: Any) -> Any:
    """Scrub a value placed straight into an audit record, leaving non-strings."""
    return scrub_control_characters(value) if isinstance(value, str) else value


def _describe_actor(actor: Any) -> dict:
    """Normalize an actor into ``{user_id, username, role}``.

    Accepts a ``User`` model instance, a mapping, a plain string, or ``None``.
    Never includes password hashes or tokens.
    """
    if actor is None:
        return {"user_id": None, "username": "anonymous", "role": None}

    if isinstance(actor, str):
        return {"user_id": None, "username": actor, "role": None}

    if isinstance(actor, Mapping):
        return {
            "user_id": actor.get("id"),
            "username": actor.get("email") or actor.get("full_name"),
            "role": actor.get("role"),
        }

    # Duck-typed ORM object (app.models.user.User). Attribute access can raise
    # on a detached or lazily-loaded instance, so degrade to an unknown actor
    # rather than losing the audit record entirely.
    try:
        return {
            "user_id": _clean(getattr(actor, "id", None)),
            "username": _clean(getattr(actor, "email", None) or getattr(actor, "full_name", None)),
            "role": _clean(getattr(actor, "role", None)),
        }
    except Exception:
        return {"user_id": None, "username": "<unresolvable-actor>", "role": None}


def _client_ip_from_request(request: Any) -> str | None:
    """Best-effort client IP.

    ``X-Forwarded-For`` is honoured only for its first entry and is
    attacker-controllable unless a trusted proxy overwrites it - it is recorded
    for investigative value, not treated as authoritative.
    """
    if request is None:
        return None
    try:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        return request.client.host if request.client else None
    except Exception:  # pragma: no cover - audit must never break a request
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def audit(
    action: "AuditAction | str",
    actor: Any = None,
    resource: str | None = None,
    outcome: "AuditOutcome | str" = AuditOutcome.SUCCESS,
    request: Any = None,
    client_ip: str | None = None,
    request_id: str | None = None,
    http_method: str | None = None,
    endpoint: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Emit one structured audit record.

    Every field is optional except ``action`` - pass a Starlette ``Request`` and
    the client IP, request ID, HTTP method and endpoint are filled in
    automatically.

    This function never raises: a failure to audit must not break the request
    it is auditing. Failures are reported on the application logger instead.
    """
    try:
        action_value = action.value if isinstance(action, AuditAction) else str(action)
        outcome_value = outcome.value if isinstance(outcome, AuditOutcome) else str(outcome)

        actor_info = _describe_actor(actor)

        resolved_ip = client_ip or _client_ip_from_request(request)
        resolved_request_id = request_id or request_id_var.get("") or None
        if not resolved_request_id and request is not None:
            resolved_request_id = getattr(getattr(request, "state", None), "request_id", None)

        resolved_method = http_method
        resolved_endpoint = endpoint
        if request is not None:
            try:
                resolved_method = resolved_method or request.method
                resolved_endpoint = resolved_endpoint or request.url.path
            except Exception:  # pragma: no cover
                pass

        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action_value,
            "outcome": outcome_value,
            "user_id": actor_info["user_id"],
            "username": actor_info["username"],
            "role": actor_info["role"],
            "request_id": resolved_request_id,
            "client_ip": resolved_ip,
            "http_method": resolved_method,
            "endpoint": resolved_endpoint,
            "resource": resource,
            "metadata": redact(dict(metadata)) if metadata else {},
        }

        level = (
            logging.WARNING
            if outcome_value in (AuditOutcome.FAILURE.value, AuditOutcome.DENIED.value)
            else logging.INFO
        )
        _audit_logger.log(
            level,
            f"audit:{action_value}:{outcome_value}",
            extra={
                "audit": record,
                # Mirror correlation fields at the top level so existing log
                # queries over request_id / user_id also match audit records.
                "request_id": resolved_request_id or "",
                "user_id": actor_info["user_id"],
                "action": action_value,
            },
        )
    except Exception as exc:  # pragma: no cover - defensive
        logging.getLogger("researchsphere.audit_error").error(
            f"Failed to emit audit record: {exc}", exc_info=True
        )
