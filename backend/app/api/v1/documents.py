import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core import metrics
from app.core.audit import AuditAction, AuditOutcome, audit
from app.core.config import settings
from app.core.database import get_db
from app.core.logging import get_logger
from app.core.pagination import Page, PageParams, page_params, paginate
from app.core.security import get_current_user, resolve_workspace
from app.core.upload_security import (
    UploadValidationError,
    discard_quarantined,
    extract_extension,
    sanitize_filename,
    stream_to_quarantine,
    validate_content,
    validate_extension,
)
from app.core.workspace_access import require_workspace_role
from app.models.document import Document, DocumentStatus
from app.models.membership import WorkspaceMember
from app.models.user import User
from app.rag.vector_store import delete_document_vectors
from app.realtime import notify
from app.services.antivirus import get_scanner
from app.services.storage import (
    TEMP_PREFIX,
    ObjectNotFound,
    StorageError,
    build_document_key,
    get_storage,
    resolve_key,
)
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


#: Sort keys a client may use, mapped to columns. An allowlist rather than
#: getattr on the model: that would expose its shape and let a caller order
#: by an unindexed column to make the query expensive.
DOCUMENT_SORTS = {
    "uploadedAt": Document.created_at,
    "title": Document.original_filename,
    "size": Document.file_size,
    "status": Document.status,
    "chunkCount": Document.chunk_count,
}


def _document_payload(doc: Document, current_user: User) -> dict:
    """The row shape the frontend expects, shared by list and upload."""
    return {
        "id": doc.id,
        "title": doc.original_filename,
        "fileType": doc.file_type,
        "fileSizeKb": (doc.file_size or 0) // 1024,
        "status": doc.status,
        "chunkCount": doc.chunk_count or 0,
        "progress": doc.progress or 0,
        "tags": doc.tags or [],
        "uploadedBy": current_user.full_name,
        "uploadedAt": doc.created_at.isoformat() if doc.created_at else "",
        "version": doc.version,
        "ocrApplied": doc.file_type == "pdf",
        "folderPath": doc.folder_path,
    }


@router.get("")
async def get_documents(
    request: Request,
    workspace_id: str | None = None,
    status: str | None = Query(None, description="Filter by processing status."),
    file_type: str | None = Query(None, description="Filter by file extension."),
    folder: str | None = Query(None, description="Filter by folder path."),
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Documents in the workspace, newest first.

    Accepts a cursor as well as a page number: a workspace's document list
    grows without bound, and deep offsets on it get slower the further in
    they go, while a cursor stays flat.
    """
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        return Page(
            items=[],
            page=params.page,
            page_size=params.page_size,
            total=0,
            pages=1,
            has_next=False,
            has_previous=False,
        ).envelope()

    query = db.query(Document).filter(Document.workspace_id == ws.id)
    if status:
        query = query.filter(Document.status == status)
    if file_type:
        query = query.filter(Document.file_type == file_type.lower().lstrip("."))
    if folder:
        query = query.filter(Document.folder_path == folder)

    page = paginate(
        query,
        params,
        sortable=DOCUMENT_SORTS,
        default_sort="uploadedAt",
        tiebreaker=Document.id,
        searchable=[Document.original_filename, Document.title, Document.description],
    )
    return page.envelope([_document_payload(doc, current_user) for doc in page.items])


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
    # Uploading is a write: viewers may read a workspace but not add to it.
    require_workspace_role(request, ws, "content.write", current_user, db=db)
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

    # 2f. Validation passed - hand the file to the configured storage
    # provider. The key is generated here and is the only thing recorded:
    # where the bytes actually live is the provider's business, and a client
    # never learns it. put() is blocking network I/O for an object store, so
    # it runs off the event loop.
    storage = get_storage()
    storage_key = build_document_key(workspace_id, ext)
    try:
        stored = await run_in_threadpool(
            storage.put, storage_key, quarantine_path, content_type=mime_type
        )
    except StorageError as exc:
        # The bytes are still in quarantine and no row exists yet, so
        # discarding them leaves nothing behind to sweep up later.
        discard_quarantined(quarantine_path)
        logger.error(
            f"Could not store upload {original_name}: {exc}",
            extra={"user_id": current_user.id, "action": "document.upload.storage_failed"},
        )
        metrics.safe(metrics.uploads_total.labels(outcome="storage_error").inc)
        raise HTTPException(
            status_code=503, detail="Document storage is unavailable. Please try again."
        ) from exc

    logger.info(
        f"Upload validated and stored: {original_name} "
        f"(kind={detected_kind}, sha256={content_hash[:12]}...)",
        extra={"user_id": current_user.id, "action": "document.upload.stored"},
    )

    # 3. Create Document DB entry
    doc = Document(
        workspace_id=workspace_id,
        uploaded_by=current_user.id,
        filename=storage_key.rsplit("/", 1)[-1],
        original_filename=original_name,
        file_type=ext,
        file_size=file_size,
        # No absolute path: the key is the address, on every provider.
        file_path=None,
        storage_provider=storage.name,
        storage_key=storage_key,
        storage_metadata=stored.metadata or None,
        content_hash=content_hash,
        mime_type=mime_type,
        status=DocumentStatus.QUEUED,
        progress=0,
        folder_path=folder or "/Uploads",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    # Announced before it is handed to a worker, not after. Inline, or on a
    # fast worker, every status event through "indexed" would otherwise reach
    # the workspace ahead of the document it describes. The row is committed,
    # so what is announced here is real.
    await notify.document_created(doc, actor_id=current_user.id, uploaded_by=current_user.full_name)

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
        .join(WorkspaceMember, Document.workspace_id == WorkspaceMember.workspace_id)
        .filter(Document.id == id, WorkspaceMember.user_id == current_user.id)
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


@router.get("/{id}/download")
async def download_document(
    id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch the original file.

    Authorization is identical to reading the document: membership gets the
    row, ``content.read`` gets the bytes. A document in someone else's
    workspace is reported as missing, so ids cannot be probed.

    How the bytes are delivered depends on the provider and is invisible to
    the caller. An object store issues a short-lived signed URL and the
    client is redirected to it; a filesystem has no URL to sign, so the API
    streams the file itself. Either way the client never receives a bucket
    name it can reuse or a path on the server.
    """
    doc = (
        db.query(Document)
        .join(WorkspaceMember, Document.workspace_id == WorkspaceMember.workspace_id)
        .filter(Document.id == id, WorkspaceMember.user_id == current_user.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    require_workspace_role(request, doc.workspace_id, "content.read", current_user, db=db)

    object_key = resolve_key(doc.storage_key, doc.file_path)
    if not object_key:
        # A row with nothing to point at. Reported as missing rather than as
        # an error: there is no file to serve and never will be.
        logger.error(f"Document {doc.id} has no storage key")
        raise HTTPException(status_code=404, detail="Document file is unavailable")

    storage = get_storage()
    audit(
        action=AuditAction.DOCUMENT_DOWNLOAD,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"document:{doc.id}",
        request=request,
        metadata={"filename": doc.original_filename, "workspace_id": doc.workspace_id},
    )

    try:
        signed = await run_in_threadpool(
            storage.signed_url,
            object_key,
            expires_in=settings.SIGNED_URL_EXPIRE_SECONDS,
            filename=doc.original_filename,
        )
        if signed:
            # 307 keeps the method and, unlike 301/302, is never cached, so a
            # URL that has expired cannot be replayed from a shared cache.
            return RedirectResponse(url=signed, status_code=307)

        # No signed URL: stream the bytes. FileResponse sends them in chunks,
        # so a large PDF is never held in memory.
        local = await run_in_threadpool(_local_file_for, storage, object_key)
        return FileResponse(
            path=local,
            filename=doc.original_filename,
            media_type=doc.mime_type or "application/octet-stream",
        )
    except ObjectNotFound as exc:
        logger.error(f"Stored object missing for document {doc.id}")
        raise HTTPException(status_code=404, detail="Document file is unavailable") from exc
    except StorageError as exc:
        logger.error(f"Storage error serving document {doc.id}: {exc}")
        raise HTTPException(
            status_code=503, detail="Document storage is unavailable. Please try again."
        ) from exc


def _local_file_for(storage, key: str) -> str:
    """A real path for an object, for providers that cannot sign a URL.

    The filesystem provider hands back the file where it already is, so
    nothing is copied. Any other provider that cannot sign falls back to a
    temporary copy, which is deleted by the cleanup sweep.
    """
    local_path = getattr(storage, "local_path", None)
    if callable(local_path):
        return local_path(key)

    temp_dir = Path(settings.QUARANTINE_DIR).parent / TEMP_PREFIX
    temp_dir.mkdir(parents=True, exist_ok=True)
    destination = temp_dir / f"{uuid.uuid4().hex}-{key.rsplit('/', 1)[-1]}"
    return storage.download_to(key, str(destination))


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
        .join(WorkspaceMember, Document.workspace_id == WorkspaceMember.workspace_id)
        .filter(Document.id == id, WorkspaceMember.user_id == current_user.id)
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

    # Membership got us the document; deleting it needs more than that.
    require_workspace_role(request, doc.workspace_id, "content.delete", current_user, db=db)

    # Delete from Qdrant
    try:
        await run_in_threadpool(delete_document_vectors, doc.id)
    except Exception as e:
        # Orphaned vectors are recoverable; failing the delete is not, so this
        # is logged rather than raised.
        logger.error(f"Failed to delete Qdrant vectors for {doc.id}: {e}")

    # Remove the stored object. Works the same whichever provider holds it,
    # and tolerates a document written before storage was abstracted: the key
    # is derived from the legacy path in that case.
    object_key = resolve_key(doc.storage_key, doc.file_path)
    object_removed = False
    if object_key:
        try:
            object_removed = await run_in_threadpool(get_storage().delete, object_key)
        except StorageError as e:
            # An object left behind is recoverable -- the cleanup sweep finds
            # it once the row is gone. Refusing the delete is not.
            logger.error(f"Failed to remove stored object for {doc.id}: {e}")

    # Kept before the row goes, for the event announcing that it went.
    deleted_workspace = doc.workspace_id
    deleted_meta = {
        "filename": doc.original_filename,
        "workspace_id": doc.workspace_id,
        "chunk_count": doc.chunk_count,
        "object_removed": object_removed,
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
    await notify.document_deleted(deleted_workspace, id, actor_id=current_user.id)
    return {"message": "Document successfully deleted"}
