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


def setup_logging(level: str = "INFO") -> None:
    """Configure structured JSON logging for the application."""
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # JSON stdout handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root_logger.addHandler(handler)

    # Quiet noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger instance."""
    return logging.getLogger(f"researchsphere.{name}")
