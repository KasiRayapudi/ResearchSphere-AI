import os
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.audit import AuditAction, AuditOutcome, audit
from app.core.config import settings
from app.core.database import get_db
from app.core.logging import get_logger
from app.core.security import get_current_user, resolve_workspace
from app.core.upload_security import (
    UploadValidationError,
    discard_quarantined,
    extract_extension,
    promote_from_quarantine,
    sanitize_filename,
    stream_to_quarantine,
    validate_content,
    validate_extension,
)
from app.models.document import Document, DocumentStatus
from app.models.user import User
from app.models.workspace import Workspace
from app.rag.vector_store import delete_document_vectors
from app.services.antivirus import get_scanner
from app.worker.dispatch import enqueue_document_processing

router = APIRouter()
logger = get_logger("documents")


class DocumentResponse(BaseModel):
    id: str
    workspace_id: str
    title: str
    fileType: str
    fileSizeKb: int
    status: str
    chunkCount: int
    tags: list[str]
    uploadedBy: str
    uploadedAt: str
    version: int
    ocrApplied: bool
    folderPath: str

    class Config:
        from_attributes = True


@router.get("", response_model=list[dict])
async def get_documents(
    request: Request,
    workspace_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Find active workspace if not specified
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        return []
    workspace_id = ws.id

    documents = db.query(Document).filter(Document.workspace_id == workspace_id).all()

    return [
        {
            "id": doc.id,
            "title": doc.original_filename,
            "fileType": doc.file_type,
            "fileSizeKb": doc.file_size // 1024,
            "status": doc.status,
            "chunkCount": doc.chunk_count,
            "tags": doc.tags or [],
            "uploadedBy": current_user.full_name,
            "uploadedAt": doc.created_at.isoformat(),
            "version": doc.version,
            "ocrApplied": doc.ocr_applied,
            "folderPath": doc.folder_path,
        }
        for doc in documents
    ]


@router.post("/upload")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    workspace_id: str | None = Form(None),
    folder: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 1. Determine active workspace
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        raise HTTPException(status_code=400, detail="Create a workspace first.")
    workspace_id = ws.id

    # 2. Security pipeline: validate -> quarantine -> scan -> promote.
    #    Nothing reaches permanent storage until every check has passed.
    upload_started = time.perf_counter()
    original_name = sanitize_filename(file.filename)
    ext = extract_extension(file.filename)

    def _reject(exc: UploadValidationError):
        """Audit and translate a validation failure into an HTTP error."""
        logger.warning(
            f"Upload rejected: {exc.reason} ({original_name})",
            extra={"user_id": current_user.id, "action": "document.upload.rejected"},
        )
        audit(
            action=AuditAction.DOCUMENT_UPLOAD_REJECTED,
            actor=current_user,
            outcome=AuditOutcome.DENIED,
            resource="document:rejected",
            request=request,
            metadata={
                "filename": original_name,
                "extension": ext,
                "reason": exc.reason,
                "workspace_id": workspace_id,
            },
        )
        metrics.safe(metrics.uploads_total.labels(outcome="rejected").inc)
        metrics.safe(metrics.upload_rejections_total.labels(reason=exc.reason).inc)
        return HTTPException(status_code=exc.status_code, detail=exc.message)

    logger.info(
        f"Upload started: {original_name}",
        extra={"user_id": current_user.id, "action": "document.upload.started"},
    )

    # 2a. Extension allowlist (cheap rejection before any bytes are written)
    try:
        validate_extension(ext, settings.allowed_upload_extensions)
    except UploadValidationError as exc:
        raise _reject(exc) from exc

    # 2b. Stream into quarantine, enforcing the size cap mid-write and
    #     computing SHA-256 in the same pass.
    try:
        stored = await stream_to_quarantine(
            upload_file=file,
            quarantine_dir=settings.QUARANTINE_DIR,
            extension=ext,
            max_bytes=settings.max_upload_bytes,
        )
    except UploadValidationError as exc:
        raise _reject(exc) from exc

    quarantine_path = stored.path
    file_size = stored.size
    content_hash = stored.sha256

    # 2c. Content-based type validation - the declared extension and the
    #     client Content-Type are both untrusted.
    try:
        detected_kind, mime_type = validate_content(quarantine_path, ext)
    except UploadValidationError as exc:
        discard_quarantined(quarantine_path)
        raise _reject(exc) from exc

    # 2d. Antivirus scan while still quarantined
    scan_result = await run_in_threadpool(get_scanner().scan_file, quarantine_path)
    if scan_result.is_infected:
        discard_quarantined(quarantine_path)
        audit(
            action=AuditAction.DOCUMENT_QUARANTINE,
            actor=current_user,
            outcome=AuditOutcome.DENIED,
            resource="document:quarantined",
            request=request,
            metadata={
                "filename": original_name,
                "sha256": content_hash,
                "workspace_id": workspace_id,
                **scan_result.to_metadata(),
            },
        )
        metrics.safe(metrics.uploads_total.labels(outcome="quarantined").inc)
        raise HTTPException(
            status_code=400, detail="File failed the security scan and was rejected."
        )

    # 2e. Duplicate detection by content hash, scoped to the workspace
    if settings.ENABLE_DUPLICATE_DETECTION:
        existing = (
            db.query(Document)
            .filter(
                Document.workspace_id == workspace_id,
                Document.content_hash == content_hash,
            )
            .first()
        )
        if existing:
            # Identical content already indexed: drop the new copy rather than
            # re-embedding it. Returns 200 with the existing document so the
            # upload API contract is unchanged for callers.
            discard_quarantined(quarantine_path)
            logger.info(
                f"Duplicate upload ignored: {original_name} -> {existing.id}",
                extra={"user_id": current_user.id, "action": "document.upload.duplicate"},
            )
            audit(
                action=AuditAction.DOCUMENT_DUPLICATE,
                actor=current_user,
                outcome=AuditOutcome.SUCCESS,
                resource=f"document:{existing.id}",
                request=request,
                metadata={
                    "filename": original_name,
                    "sha256": content_hash,
                    "workspace_id": workspace_id,
                    "existing_document_id": existing.id,
                },
            )
            metrics.safe(metrics.uploads_total.labels(outcome="duplicate").inc)
            return {
                "id": existing.id,
                "title": existing.original_filename,
                "fileType": existing.file_type,
                "fileSizeKb": existing.file_size // 1024,
                "status": existing.status,
                "chunkCount": existing.chunk_count,
                "tags": existing.tags or [],
                "uploadedBy": current_user.full_name,
                "uploadedAt": existing.created_at.isoformat(),
                "version": existing.version,
                "ocrApplied": existing.ocr_applied,
                "folderPath": existing.folder_path,
                "duplicate": True,
                "message": "This document already exists in the workspace.",
            }

    # 2f. Validation passed - promote out of quarantine into permanent storage
    file_path = promote_from_quarantine(quarantine_path, settings.UPLOAD_DIR)
    logger.info(
        f"Upload validated and stored: {original_name} "
        f"(kind={detected_kind}, sha256={content_hash[:12]}...)",
        extra={"user_id": current_user.id, "action": "document.upload.stored"},
    )

    # 3. Create Document DB entry
    doc = Document(
        workspace_id=workspace_id,
        uploaded_by=current_user.id,
        filename=os.path.basename(file_path),
        original_filename=original_name,
        file_type=ext,
        file_size=file_size,
        file_path=file_path,
        content_hash=content_hash,
        mime_type=mime_type,
        status=DocumentStatus.QUEUED,
        progress=0,
        folder_path=folder or "/Uploads",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # 4. Hand the document to a worker.
    #
    # Extraction, embedding and the vector upsert used to run here, inside
    # the request. For a large PDF that is minutes of work holding a
    # connection open, well past any proxy timeout, and it fails the whole
    # upload if one dependency is briefly unavailable. The file is already
    # stored and the row already committed, so the response can be returned
    # now and the indexing observed through GET /documents/{id}/status.
    task_id = await run_in_threadpool(enqueue_document_processing, doc.id)
    db.refresh(doc)

    metrics.safe(metrics.uploads_total.labels(outcome="accepted").inc)
    metrics.safe(metrics.upload_size_bytes.labels(file_type=ext).observe, file_size)
    metrics.safe(
        metrics.upload_request_duration_seconds.observe, time.perf_counter() - upload_started
    )
    audit(
        action=AuditAction.DOCUMENT_UPLOAD,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"document:{doc.id}",
        request=request,
        metadata={
            "filename": doc.original_filename,
            "file_type": ext,
            "detected_kind": detected_kind,
            "mime_type": mime_type,
            "file_size": file_size,
            "sha256": content_hash,
            "workspace_id": workspace_id,
            "antivirus": scan_result.to_metadata(),
            "queued": task_id is not None,
            "task_id": task_id,
        },
    )

    return {
        "id": doc.id,
        "title": doc.original_filename,
        "fileType": doc.file_type,
        "fileSizeKb": doc.file_size // 1024,
        "status": doc.status,
        "chunkCount": doc.chunk_count,
        "progress": doc.progress,
        "tags": doc.tags or ["auto-indexed"],
        "uploadedBy": current_user.full_name,
        "uploadedAt": doc.created_at.isoformat(),
        "version": doc.version,
        "ocrApplied": ext == "pdf",
        "folderPath": doc.folder_path,
    }


@router.get("/{id}/status")
async def get_document_status(
    id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Progress of background indexing for one document.

    Polled by the upload UI until the document reaches a terminal state.
    Deliberately narrow: it returns only what a progress display needs, so
    it stays cheap enough to call every couple of seconds per upload.

    Ownership is resolved through the owning workspace, exactly as deletion
    is, and an unowned document is reported as missing rather than
    forbidden so ids cannot be probed.
    """
    doc = (
        db.query(Document)
        .join(Workspace, Document.workspace_id == Workspace.id)
        .filter(Document.id == id, Workspace.owner_id == current_user.id)
        .first()
    )
    if not doc:
        audit(
            action=AuditAction.PERMISSION_DENIED,
            actor=current_user,
            outcome=AuditOutcome.DENIED,
            resource=f"document:{id}",
            request=request,
            metadata={"reason": "not_owner_or_not_found", "endpoint": "status"},
        )
        raise HTTPException(status_code=404, detail="Document not found")

    return {
        "id": doc.id,
        "status": doc.status,
        "progress": doc.progress or 0,
        "chunkCount": doc.chunk_count or 0,
        "error": doc.error_message,
        "updatedAt": doc.updated_at.isoformat() if doc.updated_at else None,
        "startedAt": (doc.processing_started_at.isoformat() if doc.processing_started_at else None),
        "completedAt": (
            doc.processing_completed_at.isoformat() if doc.processing_completed_at else None
        ),
    }


@router.delete("/{id}")
async def delete_document(
    id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Ownership check: a document is reachable only through a workspace the
    # caller owns. Previously any authenticated user could delete any document
    # by id. A document that exists but belongs to someone else returns 404,
    # so ids cannot be probed.
    doc = (
        db.query(Document)
        .join(Workspace, Document.workspace_id == Workspace.id)
        .filter(Document.id == id, Workspace.owner_id == current_user.id)
        .first()
    )
    if not doc:
        audit(
            action=AuditAction.DOCUMENT_DELETE,
            actor=current_user,
            outcome=AuditOutcome.FAILURE,
            resource=f"document:{id}",
            request=request,
            metadata={"reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail="Document not found")

    # Delete from Qdrant
    try:
        await run_in_threadpool(delete_document_vectors, doc.id)
    except Exception as e:
        # Orphaned vectors are recoverable; failing the delete is not, so this
        # is logged rather than raised.
        logger.error(f"Failed to delete Qdrant vectors for {doc.id}: {e}")

    # Clean file locally
    if os.path.exists(doc.file_path):
        try:
            os.remove(doc.file_path)
        except Exception as e:
            logger.error(f"Failed to remove local file for {doc.id}: {e}")

    deleted_meta = {
        "filename": doc.original_filename,
        "workspace_id": doc.workspace_id,
        "chunk_count": doc.chunk_count,
    }
    db.delete(doc)
    db.commit()
    audit(
        action=AuditAction.DOCUMENT_DELETE,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"document:{id}",
        request=request,
        metadata=deleted_meta,
    )
    return {"message": "Document successfully deleted"}
