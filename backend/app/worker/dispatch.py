"""
Enqueueing side of document ingestion.

Kept apart from the task module so the API layer never imports the pipeline
itself, and so there is exactly one place that decides how a document gets
processed.

Deployment without a broker
---------------------------

Redis is optional throughout this codebase, and Celery cannot run without
it. Rather than failing every upload on a deployment that has no Redis, a
brokerless install falls back to processing the document inline, in a worker
thread, which is what the upload handler did before this change.

That fallback is a development convenience, not a supported production
mode: it reintroduces the request-bound ingestion this phase exists to
remove, so it logs a warning every time it is used and the API reports it
through the health endpoint. docker-compose.prod.yml provisions Redis, so a
standard production deployment always takes the queued path.
"""

from __future__ import annotations

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("worker.dispatch")


def broker_available() -> bool:
    """True when a Celery broker is configured for this deployment."""
    return bool(settings.redis_enabled)


def enqueue_document_processing(document_id: str) -> str | None:
    """Hand a document to a worker. Returns the task id, or None inline.

    Never raises: a broker that is briefly unreachable must not lose an
    upload that has already been accepted and stored. When the hand-off
    fails, the document is processed inline so the user still gets an
    indexed document, and the failure is logged.
    """
    from app.worker.tasks import process_document

    if not broker_available():
        logger.warning(
            "No Celery broker is configured (REDIS_URL is empty), so document "
            f"{document_id} is being indexed inline. Set REDIS_URL and run a "
            "worker to process documents in the background.",
            extra={"action": "document.enqueue.inline", "resource_type": "document"},
        )
        _process_inline(document_id)
        return None

    try:
        result = process_document.delay(document_id)
        logger.info(
            f"Queued document {document_id} for indexing (task {result.id})",
            extra={"action": "document.enqueue", "resource_type": "document"},
        )
        return result.id
    except Exception as exc:
        logger.error(
            f"Could not queue document {document_id} ({exc}); indexing inline instead",
            extra={"action": "document.enqueue.failed", "resource_type": "document"},
            exc_info=True,
        )
        _process_inline(document_id)
        return None


def _process_inline(document_id: str) -> None:
    """Run the ingestion task in this process.

    Calling the task function directly bypasses the broker but runs exactly
    the same code, including the status transitions, so a document processed
    this way is indistinguishable afterwards from one a worker handled.
    """
    from app.worker.tasks import process_document

    try:
        process_document.apply(args=[document_id])
    except Exception:
        # apply() surfaces the task's own failure handling, which has already
        # marked the document failed. Nothing further to do here, and this
        # must not propagate into the HTTP response.
        logger.error(f"Inline indexing of {document_id} failed", exc_info=True)
