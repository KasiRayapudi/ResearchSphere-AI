"""
FastAPI exception handlers for ResearchSphere AI.
Provides JSON error responses using the `format_error_response` utility.
"""
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from app.core.exceptions import (
    AppException,
    ResourceNotFoundException,
    AuthenticationException,
    AuthorizationException,
    ValidationException,
    RateLimitException,
    RAGProcessingException,
    FileValidationException,
    format_error_response,
)


async def app_exception_handler(request: Request, exc: AppException):
    """Handle custom `AppException` and subclasses, returning a JSON body."""
    return JSONResponse(
        status_code=exc.status_code,
        content=format_error_response(exc, request_id=getattr(request.state, "request_id", "")),
    )


async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle generic FastAPI `HTTPException` by wrapping it in `AppException`."""
    app_exc = AppException(
        message=exc.detail if isinstance(exc.detail, str) else "HTTP error",
        code="HTTP_ERROR",
        status_code=exc.status_code,
    )
    return await app_exception_handler(request, app_exc)


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle request validation errors (422) with detailed field errors."""
    details = [
        {"loc": err.get("loc"), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    app_exc = ValidationException(message="Validation error", details=details)
    return await app_exception_handler(request, app_exc)
async def generic_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions and return a standardized JSON response."""
    from app.core.logging import get_logger
    logger = get_logger("exception")
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    app_exc = AppException(message="Internal server error", code="INTERNAL_ERROR", status_code=500)
    return await app_exception_handler(request, app_exc)
