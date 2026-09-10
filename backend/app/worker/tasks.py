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

import os
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from app.core import metrics
from app.core.audit import AuditAction, AuditOutcome, audit
from app.core.config import settings
from app.core.logging import get_logger
from app.core.tracking import capture_exception
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.rag.document_processor import chunk_text, clean_text, extract_text
from app.rag.embeddings import embed_texts
from app.rag.vector_store import upsert_chunks
from app.services.storage import (
    DOCUMENT_PREFIX,
    TEMP_PREFIX,
    ObjectNotFound,
    StorageError,
    get_storage,
    resolve_key,
)
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


@contextmanager
def _document_file(doc: Document):
    """A readable local path for a document, for as long as the block runs.

    Extraction reads a real file: PyMuPDF and python-docx open paths, not
    streams. The filesystem provider already has one and hands it over
    untouched; an object store is fetched into a temporary file that is
    removed on the way out, whether the ingest succeeded or raised.
    """
    key = resolve_key(doc.storage_key, doc.file_path)
    if not key:
        raise ObjectNotFound(f"Document {doc.id} has no storage key")

    storage = get_storage()
    local_path = getattr(storage, "local_path", None)
    if callable(local_path):
        yield local_path(key)
        return

    suffix = os.path.splitext(key)[1]
    handle, temp_path = tempfile.mkstemp(prefix="ingest-", suffix=suffix)
    os.close(handle)
    try:
        yield storage.download_to(key, temp_path)
    finally:
        try:
            os.unlink(temp_path)
        except OSError as exc:
            # The sweep collects it later. A leftover temp file must not turn
            # a successful ingest into a failed one.
            logger.warning(f"Could not remove temporary file for {doc.id}: {exc}")


def _ingest(db, doc: Document) -> int:
    """Run the pipeline for one document. Returns the chunk count.

    Separated from the task so it can be exercised directly, and so the retry
    bookkeeping above stays readable.
    """
    with _document_file(doc) as source_path:
        text = extract_text(source_path, doc.file_type)
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


# ---------------------------------------------------------------------------
# Storage housekeeping
# ---------------------------------------------------------------------------
#
# Three kinds of rubbish accumulate around document storage:
#
# * objects whose document row is gone -- a delete where the database commit
#   succeeded and the object removal did not,
# * quarantine and temporary files left by a request that died mid-flight,
# * bytes belonging to documents that failed indexing and will never be read.
#
# The sweep is deliberately timid. It looks only under the document prefix,
# ignores anything younger than a grace period (an upload writes the object
# before it commits the row), checks every candidate against the database,
# and stops after a bounded number of deletions -- so a mistake is capped and
# visible in the statistics instead of emptying a bucket.


def _live_keys(db) -> set[str]:
    """Every storage key the database still refers to.

    Includes keys derived from legacy ``file_path`` rows, so a document
    uploaded before storage was abstracted is not mistaken for an orphan.
    """
    keys: set[str] = set()
    for storage_key, file_path in db.query(Document.storage_key, Document.file_path):
        key = resolve_key(storage_key, file_path)
        if key:
            keys.add(key)
    return keys


def _sweep_orphans(db, stats: dict) -> None:
    storage = get_storage()
    # Aware, unlike _utcnow(): object timestamps come from the storage
    # backend and carry a timezone, while the database columns do not.
    cutoff = datetime.now(UTC) - timedelta(hours=max(1, settings.STORAGE_CLEANUP_GRACE_HOURS))
    budget = max(0, settings.STORAGE_CLEANUP_MAX_DELETES)
    live = _live_keys(db)

    for obj in storage.list(prefix=DOCUMENT_PREFIX, limit=10000):
        stats["objects_scanned"] += 1
        if obj.key in live:
            continue
        modified = obj.modified_at
        if modified is not None:
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=UTC)
            if modified > cutoff:
                # Young enough that an upload may still be committing its row.
                stats["orphans_skipped_recent"] += 1
                continue
        if stats["orphans_deleted"] >= budget:
            stats["orphans_over_budget"] += 1
            continue
        if storage.delete(obj.key):
            stats["orphans_deleted"] += 1
            stats["bytes_reclaimed"] += obj.size
            logger.info(
                f"Removed orphaned object {obj.key}",
                extra={"action": "storage.cleanup.orphan", "resource_type": "storage"},
            )


def _sweep_temp_files(stats: dict) -> None:
    """Remove abandoned quarantine and temporary files.

    Quarantine holds uploads that are still being validated, so only files
    older than the retention window are touched: anything newer may belong to
    a request that is still running.
    """
    cutoff = time.time() - max(1, settings.STORAGE_TEMP_RETENTION_HOURS) * 3600
    quarantine = Path(settings.QUARANTINE_DIR)
    for directory in (quarantine, quarantine.parent / TEMP_PREFIX):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if not path.is_file():
                continue
            try:
                stat = path.stat()
                if stat.st_mtime > cutoff:
                    continue
                path.unlink()
            except OSError as exc:
                # A file still held open is retried on the next run.
                logger.warning(f"Could not remove temporary file {path.name}: {exc}")
                continue
            stats["temp_files_deleted"] += 1
            stats["bytes_reclaimed"] += stat.st_size


def _sweep_failed_uploads(db, stats: dict) -> None:
    """Release the bytes of documents that failed indexing.

    The row stays -- the user should still see that the upload failed and
    why. Only the stored object goes, and the key goes with it so the
    download endpoint reports the file as unavailable rather than pointing at
    something that is no longer there.
    """
    if settings.STORAGE_FAILED_RETENTION_DAYS <= 0:
        return

    storage = get_storage()
    # Naive, to match processing_completed_at.
    cutoff = _utcnow() - timedelta(days=settings.STORAGE_FAILED_RETENTION_DAYS)
    failed = (
        db.query(Document)
        .filter(
            Document.status == DocumentStatus.FAILED,
            Document.storage_key.isnot(None),
            Document.processing_completed_at.isnot(None),
            Document.processing_completed_at < cutoff,
        )
        .limit(max(0, settings.STORAGE_CLEANUP_MAX_DELETES))
        .all()
    )
    for doc in failed:
        try:
            storage.delete(doc.storage_key)
        except StorageError as exc:
            logger.warning(f"Could not purge failed upload {doc.id}: {exc}")
            continue
        metadata = dict(doc.storage_metadata or {})
        metadata["purged_at"] = _utcnow().isoformat()
        doc.storage_key = None
        doc.storage_metadata = metadata
        stats["failed_uploads_purged"] += 1
    if failed:
        db.commit()


@celery_app.task(name="storage.cleanup")
def cleanup_storage() -> dict:
    """Periodic storage housekeeping. Returns what it did.

    Each sweep is guarded independently: one failing must not stop the
    others, because the whole point of this task is that it keeps running
    unattended.
    """
    stats: dict = {
        "objects_scanned": 0,
        "orphans_deleted": 0,
        "orphans_skipped_recent": 0,
        "orphans_over_budget": 0,
        "temp_files_deleted": 0,
        "failed_uploads_purged": 0,
        "bytes_reclaimed": 0,
        "errors": [],
    }
    db = _session()
    try:
        for label, sweep in (
            ("orphans", lambda: _sweep_orphans(db, stats)),
            ("temp_files", lambda: _sweep_temp_files(stats)),
            ("failed_uploads", lambda: _sweep_failed_uploads(db, stats)),
        ):
            try:
                sweep()
            except Exception as exc:
                logger.error(f"Storage cleanup sweep {label!r} failed: {exc}")
                capture_exception(exc)
                stats["errors"].append(label)
    finally:
        db.close()

    logger.info(
        "Storage cleanup: "
        f"scanned={stats['objects_scanned']} orphans={stats['orphans_deleted']} "
        f"temp={stats['temp_files_deleted']} failed={stats['failed_uploads_purged']} "
        f"reclaimed={stats['bytes_reclaimed']}B",
        extra={"action": "storage.cleanup", "resource_type": "storage"},
    )
    return stats
