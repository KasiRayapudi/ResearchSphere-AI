"""
Background document ingestion.

The work that used to run inside the upload request lives here: extraction,
chunking, embedding and the vector upsert. The HTTP handler now stores the
file, records a queued document and returns, so a large PDF can no longer
hold a connection open past a proxy timeout.

State machine
-------------

    queued -> processing -> indexed
                         -> failed

``queued`` is written by the upload handler. Everything else is written
here. ``indexed`` and ``failed`` are terminal: nothing moves a document out
of them without a new request.

Failure handling
----------------

Transient faults -- Qdrant unreachable, a model download that lost its
network -- are retried with exponential backoff. Only once retries are
exhausted is the document marked ``failed``, with the exception recorded on
the row so the operator and the user see the same reason.

A worker killed outright runs no handler at all. Two mechanisms cover that:
``task_acks_late`` makes the broker redeliver the task, and
``reap_stalled_documents`` fails anything that has been ``processing``
longer than any real ingest could take. Between them a document cannot sit
in ``processing`` forever, which is the failure mode that matters most: a
user watching a spinner that will never resolve.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.core import metrics
from app.core.audit import AuditAction, AuditOutcome, audit
from app.core.logging import get_logger
from app.core.tracking import capture_exception
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.rag.document_processor import chunk_text, clean_text, extract_text
from app.rag.embeddings import embed_texts
from app.rag.vector_store import upsert_chunks
from app.worker.celery_app import celery_app

logger = get_logger("worker.documents")

#: Progress reported at each stage boundary. Coarse on purpose: it tells the
#: user which phase is running, it does not animate a bar smoothly.
PROGRESS_EXTRACTED = 20
PROGRESS_CHUNKED = 35
PROGRESS_EMBEDDED = 70
PROGRESS_PERSISTED = 90
PROGRESS_DONE = 100

MAX_RETRIES = 3
#: Backoff base in seconds: 2s, 4s, 8s. Long enough for a dependency to come
#: back, short enough that someone waiting on an upload still gets an answer.
RETRY_BASE_SECONDS = 2
RETRY_MAX_SECONDS = 60

#: A document processing for longer than this has lost its worker. The hard
#: task time limit is 600s, so nothing caught here can still be running.
STALLED_AFTER = timedelta(minutes=30)


def _utcnow() -> datetime:
    """Naive UTC, matching the DateTime columns used across the models."""
    return datetime.now(UTC).replace(tzinfo=None)


def _session():
    """A worker-owned database session.

    Imported lazily so importing this module does not require the database to
    be reachable, which keeps ``celery inspect`` and test collection cheap.
    """
    from app.core.database import SessionLocal

    return SessionLocal()


def _set_state(
    db, doc: Document, *, status: str | None = None, progress: int | None = None
) -> None:
    """Persist a status/progress step immediately.

    Committed as its own transaction rather than batched at the end: the
    whole point of these fields is to be readable by a client polling the
    status endpoint while the work is still running.
    """
    if status is not None:
        doc.status = status
    if progress is not None:
        doc.progress = progress
    db.commit()


class DocumentIngestTask(Task):
    """Base task that records a permanent failure on the document row.

    Celery calls ``on_failure`` only after the final attempt, so a document is
    never shown as failed while a retry is still pending.
    """

    def on_failure(self, exc, task_id, args, kwargs, einfo):  # noqa: ANN001, D102
        document_id = (args[0] if args else kwargs.get("document_id")) or None
        if not document_id:
            logger.error(f"Ingest task {task_id} failed with no document id: {exc}")
            return

        db = _session()
        try:
            doc = db.get(Document, document_id)
            if doc is None:
                return
            doc.status = DocumentStatus.FAILED
            doc.error_message = f"{type(exc).__name__}: {exc}"[:1000]
            doc.processing_completed_at = _utcnow()
            db.commit()

            metrics.safe(metrics.uploads_total.labels(outcome="failed").inc)
            capture_exception(exc, task="documents.process", document_id=document_id)
            logger.error(
                f"Indexing failed permanently for {document_id}: {exc}",
                extra={"action": "document.index.failed", "resource_type": "document"},
            )
            audit(
                action=AuditAction.DOCUMENT_INDEXED,
                actor=doc.uploaded_by,
                outcome=AuditOutcome.FAILURE,
                resource=f"document:{document_id}",
                metadata={
                    "workspace_id": doc.workspace_id,
                    "reason": str(exc)[:200],
                    "attempts": self.request.retries + 1,
                },
            )
        finally:
            db.close()


@celery_app.task(bind=True, base=DocumentIngestTask, name="documents.process")
def process_document(self, document_id: str) -> dict:
    """Extract, chunk, embed and index one document."""
    started = time.perf_counter()
    db = _session()
    try:
        doc = db.get(Document, document_id)
        if doc is None:
            # Deleted between enqueue and pickup. Not an error.
            logger.info(f"Document {document_id} no longer exists; nothing to index")
            return {"document_id": document_id, "status": "missing"}

        if doc.status == DocumentStatus.INDEXED:
            # A redelivered task for work that already completed.
            logger.info(f"Document {document_id} is already indexed; skipping")
            return {"document_id": document_id, "status": doc.status}

        doc.processing_started_at = _utcnow()
        doc.processing_completed_at = None
        doc.error_message = None
        _set_state(db, doc, status=DocumentStatus.PROCESSING, progress=0)

        try:
            chunk_count = _ingest(db, doc)
        except SoftTimeLimitExceeded:
            # The document is pathological rather than the dependency being
            # briefly unavailable, so retrying only burns another worker slot.
            raise
        except Exception as exc:
            if self.request.retries >= MAX_RETRIES:
                raise
            countdown = min(RETRY_BASE_SECONDS * (2**self.request.retries), RETRY_MAX_SECONDS)
            logger.warning(
                f"Indexing {document_id} failed ({exc}); retry "
                f"{self.request.retries + 1}/{MAX_RETRIES} in {countdown}s"
            )
            raise self.retry(exc=exc, countdown=countdown) from exc

        elapsed = time.perf_counter() - started
        metrics.safe(metrics.documents_indexed_total.inc)
        metrics.safe(metrics.document_chunks_total.inc, chunk_count)
        metrics.safe(metrics.upload_duration_seconds.observe, elapsed)
        logger.info(
            f"Indexed {doc.original_filename}: {chunk_count} chunks in {elapsed:.1f}s",
            extra={"action": "document.indexed", "resource_type": "document"},
        )
        audit(
            action=AuditAction.DOCUMENT_INDEXED,
            actor=doc.uploaded_by,
            outcome=AuditOutcome.SUCCESS,
            resource=f"document:{doc.id}",
            metadata={
                "filename": doc.original_filename,
                "workspace_id": doc.workspace_id,
                "chunk_count": chunk_count,
                "duration_seconds": round(elapsed, 2),
                "attempt": self.request.retries + 1,
            },
        )
        return {"document_id": doc.id, "status": doc.status, "chunk_count": chunk_count}
    finally:
        db.close()


def _ingest(db, doc: Document) -> int:
    """Run the pipeline for one document. Returns the chunk count.

    Separated from the task so it can be exercised directly, and so the retry
    bookkeeping above stays readable.
    """
    text = extract_text(doc.file_path, doc.file_type)
    cleaned = clean_text(text)
    _set_state(db, doc, progress=PROGRESS_EXTRACTED)

    chunks = chunk_text(cleaned)
    _set_state(db, doc, progress=PROGRESS_CHUNKED)

    contents = [c["content"] for c in chunks]
    embeddings = embed_texts(contents)
    _set_state(db, doc, progress=PROGRESS_EMBEDDED)

    # Clear anything a previous attempt left behind so a retry cannot
    # duplicate chunks.
    db.query(DocumentChunk).filter(DocumentChunk.document_id == doc.id).delete(
        synchronize_session=False
    )
    db.commit()

    chunk_rows = []
    for idx, chunk in enumerate(chunks):
        row = DocumentChunk(
            document_id=doc.id,
            chunk_index=idx,
            content=chunk["content"],
            token_count=chunk["token_count"],
        )
        db.add(row)
        chunk_rows.append(row)
    db.commit()

    for idx, row in enumerate(chunk_rows):
        db.refresh(row)
        chunks[idx]["db_id"] = row.id
    _set_state(db, doc, progress=PROGRESS_PERSISTED)

    point_ids = upsert_chunks(
        chunks=chunks,
        embeddings=embeddings,
        document_id=doc.id,
        workspace_id=doc.workspace_id,
    )
    for idx, point_id in enumerate(point_ids):
        chunk_rows[idx].qdrant_point_id = point_id

    doc.chunk_count = len(chunks)
    doc.indexed_at = _utcnow()
    doc.processing_completed_at = doc.indexed_at
    _set_state(db, doc, status=DocumentStatus.INDEXED, progress=PROGRESS_DONE)
    return len(chunks)


@celery_app.task(name="documents.reap_stalled")
def reap_stalled_documents() -> dict:
    """Fail documents whose worker died without running a failure handler.

    A killed process runs no ``on_failure``, so without this a document would
    sit in ``processing`` until someone noticed. The cutoff is well past the
    hard task time limit, so anything caught here genuinely has no worker
    behind it.
    """
    cutoff = _utcnow() - STALLED_AFTER
    db = _session()
    try:
        stalled = (
            db.query(Document)
            .filter(
                Document.status == DocumentStatus.PROCESSING,
                Document.processing_started_at.isnot(None),
                Document.processing_started_at < cutoff,
            )
            .all()
        )
        for doc in stalled:
            doc.status = DocumentStatus.FAILED
            doc.error_message = (
                "Indexing did not complete: the worker processing this document "
                "stopped responding. Upload the document again to retry."
            )
            doc.processing_completed_at = _utcnow()
            logger.error(
                f"Reaped stalled document {doc.id} "
                f"(processing since {doc.processing_started_at})",
                extra={"action": "document.index.stalled", "resource_type": "document"},
            )
        if stalled:
            db.commit()
        return {"reaped": len(stalled)}
    finally:
        db.close()
