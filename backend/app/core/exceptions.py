"""
Application-specific exceptions with structured error response formatting.
"""
from typing import Any, Optional, List
from datetime import datetime


class AppException(Exception):
    """Base exception for ResearchSphere AI."""

    def __init__(
        self,
        message: str = "An unexpected error occurred",
        code: str = "INTERNAL_ERROR",
        status_code: int = 500,
        details: Optional[List[dict]] = None,
    ):
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or []
        super().__init__(self.message)


class ResourceNotFoundException(AppException):
    def __init__(self, resource: str = "Resource", resource_id: str = ""):
        super().__init__(
            message=f"{resource} not found: {resource_id}",
            code="RESOURCE_NOT_FOUND",
            status_code=404,
        )


class AuthenticationException(AppException):
    def __init__(self, message: str = "Authentication failed"):
        super().__init__(
            message=message,
            code="AUTHENTICATION_FAILED",
            status_code=401,
        )


class AuthorizationException(AppException):
    def __init__(self, message: str = "Insufficient permissions"):
        super().__init__(
            message=message,
            code="AUTHORIZATION_FAILED",
            status_code=403,
        )


class ValidationException(AppException):
    def __init__(self, message: str = "Validation error", details: Optional[List[dict]] = None):
        super().__init__(
            message=message,
            code="VALIDATION_ERROR",
            status_code=422,
            details=details,
        )


class RateLimitException(AppException):
    def __init__(self, message: str = "Rate limit exceeded. Please try again later."):
        super().__init__(
            message=message,
            code="RATE_LIMIT_EXCEEDED",
            status_code=429,
        )


class RAGProcessingException(AppException):
    def __init__(self, message: str = "RAG pipeline processing failed"):
        super().__init__(
            message=message,
            code="RAG_PROCESSING_ERROR",
            status_code=500,
        )


class FileValidationException(AppException):
    def __init__(self, message: str = "File validation failed"):
        super().__init__(
            message=message,
            code="FILE_VALIDATION_ERROR",
            status_code=400,
        )


def format_error_response(
    exc: AppException,
    request_id: str = "",
) -> dict:
    """Build a standardized JSON error response body."""
    return {
        "error": {
            "code": exc.code,
            "message": exc.message,
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "details": exc.details,
        }
    }
