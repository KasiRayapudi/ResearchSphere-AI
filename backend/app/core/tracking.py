"""
Error-tracking hooks.

No vendor SDK is bundled. Sentry, Rollbar and friends each pull a substantial
dependency tree and require an account, so this module defines the seam and
ships a logging-only backend. Wiring a real provider means implementing
``ErrorTracker`` and registering it, with no changes to any call site.

To add Sentry::

    class SentryTracker(ErrorTracker):
        name = "sentry"
        def __init__(self, dsn, sample_rate):
            import sentry_sdk
            sentry_sdk.init(dsn=dsn, traces_sample_rate=sample_rate)
        def capture_exception(self, exc, context=None):
            import sentry_sdk
            with sentry_sdk.push_scope() as scope:
                for key, value in (context or {}).items():
                    scope.set_extra(key, value)
                sentry_sdk.capture_exception(exc)

then register it in ``_BACKENDS`` and set ``ERROR_TRACKING_DSN``.
"""

from abc import ABC, abstractmethod
from typing import Any

from app.core.audit import redact
from app.core.config import settings
from app.core.logging import get_logger, request_id_var

logger = get_logger("tracking")


class ErrorTracker(ABC):
    """Destination for unhandled exceptions and explicit error events."""

    name: str = "base"

    @abstractmethod
    def capture_exception(self, exc: BaseException, context: dict[str, Any] | None = None) -> None:
        """Record an exception with optional structured context."""

    def capture_message(
        self, message: str, level: str = "error", context: dict[str, Any] | None = None
    ) -> None:
        """Record an event with no exception attached."""
        logger.log(
            {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(level, 40),
            message,
            extra={"tracking_context": redact(context or {})},
        )


class LoggingTracker(ErrorTracker):
    """Default backend: writes to the structured application log.

    Not a no-op - exceptions are recorded with full context and correlated by
    request id, they simply are not shipped to a third party.
    """

    name = "logging"

    def capture_exception(self, exc: BaseException, context: dict[str, Any] | None = None) -> None:
        payload = redact(dict(context or {}))
        payload.setdefault("request_id", request_id_var.get("") or None)
        logger.error(
            f"Captured exception: {type(exc).__name__}: {exc}",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"tracking_context": payload},
        )


#: Registered backends. Add real providers here.
_BACKENDS: dict[str, type[ErrorTracker]] = {
    "logging": LoggingTracker,
}

_tracker: ErrorTracker | None = None


def get_tracker() -> ErrorTracker:
    """Return the configured tracker (singleton)."""
    global _tracker
    if _tracker is None:
        # A DSN is accepted for forward compatibility, but with no provider
        # registered the logging backend is used and the mismatch is stated
        # rather than silently swallowing errors.
        if settings.ERROR_TRACKING_DSN:
            scheme = settings.ERROR_TRACKING_DSN.split("://", 1)[0].lower()
            backend = _BACKENDS.get(scheme)
            if backend is None:
                logger.warning(
                    f"ERROR_TRACKING_DSN is set for '{scheme}', but no such backend is "
                    "registered. Falling back to structured logging; see "
                    "app/core/tracking.py to add one."
                )
                backend = LoggingTracker
        else:
            backend = LoggingTracker
        _tracker = backend()
        logger.info(f"Error tracking backend: {_tracker.name}")
    return _tracker


def reset_tracker() -> None:
    """Clear the cached tracker (used by tests)."""
    global _tracker
    _tracker = None


def capture_exception(exc: BaseException, **context: Any) -> None:
    """Convenience wrapper. Never raises - reporting must not mask the error."""
    try:
        get_tracker().capture_exception(exc, context)
    except Exception:  # pragma: no cover - defensive
        logger.error("Error tracker failed while capturing an exception", exc_info=True)
