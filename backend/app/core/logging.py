"""
Structured JSON Logging with Request ID correlation for ResearchSphere AI.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime

# Context var for request ID propagation
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


class JSONFormatter(logging.Formatter):
    """Formats log records as structured JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add request ID if available. An explicit `extra={"request_id": ...}`
        # wins over the contextvar, so middleware running outside the
        # RequestIDMiddleware scope can still correlate its log lines.
        req_id = getattr(record, "request_id", "") or request_id_var.get("")
        if req_id:
            log_entry["request_id"] = req_id
        if hasattr(record, "client_ip"):
            log_entry["client_ip"] = record.client_ip

        # Add extra fields
        if hasattr(record, "duration_ms"):
            log_entry["duration_ms"] = record.duration_ms
        if hasattr(record, "user_id"):
            log_entry["user_id"] = record.user_id
        if hasattr(record, "action"):
            log_entry["action"] = record.action
        if hasattr(record, "resource_type"):
            log_entry["resource_type"] = record.resource_type
        if hasattr(record, "status_code"):
            log_entry["status_code"] = record.status_code
        # Structured audit payload (see app/core/audit.py). Emitted as a nested
        # object so audit records stay machine-parseable and can be routed to a
        # separate sink without changing the surrounding log format.
        if hasattr(record, "audit"):
            log_entry["audit"] = record.audit

        # Add exception info
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else "Unknown",
                "message": str(record.exc_info[1]),
            }

        return json.dumps(log_entry, default=str)


def setup_logging(level: str | None = None) -> None:
    """Configure structured JSON logging for the application.

    stdout is always configured, which is what a container wants. A rotating
    file handler is added as well when LOG_FILE is set, for deployments that
    are not containerised and have no log collector in front of them.
    """
    # Imported lazily: this module is imported by config's dependents, and a
    # top-level import would be circular.
    try:
        from app.core.config import settings

        resolved_level = level or settings.LOG_LEVEL
        log_file = settings.LOG_FILE
        max_bytes = settings.LOG_FILE_MAX_BYTES
        backup_count = settings.LOG_FILE_BACKUP_COUNT
    except Exception:  # pragma: no cover - configuration not importable yet
        resolved_level = level or "INFO"
        log_file, max_bytes, backup_count = "", 10 * 1024 * 1024, 5

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, str(resolved_level).upper(), logging.INFO))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # JSON stdout handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root_logger.addHandler(handler)

    # Optional rotating file handler.
    if log_file:
        try:
            from logging.handlers import RotatingFileHandler
            from pathlib import Path

            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(JSONFormatter())
            root_logger.addHandler(file_handler)
        except Exception as exc:  # pragma: no cover
            # An unwritable log path must not stop the application from
            # starting; stdout logging still works.
            root_logger.warning(f"Could not open log file {log_file}: {exc}")

    # Quiet noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger instance."""
    return logging.getLogger(f"researchsphere.{name}")
