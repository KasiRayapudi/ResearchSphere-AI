from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel
from app.core.database import get_db
from app.core.security import get_current_user, resolve_workspace
from app.models.user import User
from app.models.workspace import Workspace
from app.models.document import Document, DocumentChunk
from app.rag.document_processor import extract_text, clean_text, chunk_text
from app.rag.embeddings import embed_texts
from app.rag.vector_store import upsert_chunks, delete_document_vectors
from app.core.config import settings
from app.core.audit import audit, AuditAction, AuditOutcome
from app.core.logging import get_logger
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
from app.services.antivirus import get_scanner
import os
import shutil
from datetime import datetime

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
    tags: List[str]
    uploadedBy: str
    uploadedAt: str
    version: int
    ocrApplied: bool
    folderPath: str

    class Config:
        from_attributes = True

@router.get("", response_model=List[dict])
async def get_documents(
    request: Request,
    workspace_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
    workspace_id: Optional[str] = Form(None),
    folder: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # 1. Determine active workspace
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        raise HTTPException(status_code=400, detail="Create a workspace first.")
    workspace_id = ws.id

    # 2. Security pipeline: validate -> quarantine -> scan -> promote.
    #    Nothing reaches permanent storage until every check has passed.
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
        return HTTPException(status_code=exc.status_code, detail=exc.message)

    logger.info(
        f"Upload started: {original_name}",
        extra={"user_id": current_user.id, "action": "document.upload.started"},
    )

    # 2a. Extension allowlist (cheap rejection before any bytes are written)
    try:
        validate_extension(ext, settings.allowed_upload_extensions)
    except UploadValidationError as exc:
        raise _reject(exc)

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
        raise _reject(exc)

    quarantine_path = stored.path
    file_size = stored.size
    content_hash = stored.sha256

    # 2c. Content-based type validation - the declared extension and the
    #     client Content-Type are both untrusted.
    try:
        detected_kind, mime_type = validate_content(quarantine_path, ext)
    except UploadValidationError as exc:
        discard_quarantined(quarantine_path)
        raise _reject(exc)

    # 2d. Antivirus scan while still quarantined
    scan_result = get_scanner().scan_file(quarantine_path)
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
        status="processing",
        folder_path=folder or "/Uploads",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    
    
    try:
        # 4. Extract and clean text
        text = extract_text(file_path, ext)
        cleaned = clean_text(text)
        
        # 5. Chunk text
        chunks = chunk_text(cleaned)
        
        # 6. Embed chunks
        contents = [c["content"] for c in chunks]
        embeddings = embed_texts(contents)
        
        # 7. Write chunks to DB and Vector DB
        chunk_db_objects = []
        for idx, chunk in enumerate(chunks):
            db_chunk = DocumentChunk(
                document_id=doc.id,
                chunk_index=idx,
                content=chunk["content"],
                token_count=chunk["token_count"],
            )
            db.add(db_chunk)
            chunk_db_objects.append(db_chunk)
            
        db.commit()
        
        # Update point payload mapping
        qdrant_payloads = []
        for idx, db_chunk in enumerate(chunk_db_objects):
            chunks[idx]["db_id"] = db_chunk.id
            db.refresh(db_chunk)
            
        # 8. Upsert into Qdrant
        point_ids = upsert_chunks(
            chunks=chunks,
            embeddings=embeddings,
            document_id=doc.id,
            workspace_id=workspace_id,
        )
        
        # Save Qdrant point IDs on chunks
        for idx, point_id in enumerate(point_ids):
            chunk_db_objects[idx].qdrant_point_id = point_id
            
        # 9. Update Document status
        doc.status = "indexed"
        doc.chunk_count = len(chunks)
        doc.indexed_at = datetime.utcnow()
        db.commit()
        
    except Exception as e:
        doc.status = "failed"
        doc.error_message = str(e)
        db.commit()
        # Clean up file on failure. Must never raise: on Windows an extractor
        # that failed mid-parse can still hold the handle, and letting that
        # PermissionError escape would mask the real processing error and
        # bypass the audit record below.
        discard_quarantined(file_path)
        audit(
            action=AuditAction.DOCUMENT_UPLOAD,
            actor=current_user,
            outcome=AuditOutcome.FAILURE,
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
                "reason": str(e)[:200],
            },
        )
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process document: {str(e)}"
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
            "chunk_count": doc.chunk_count,
            "workspace_id": workspace_id,
            "antivirus": scan_result.to_metadata(),
        },
    )
        
    return {
        "id": doc.id,
        "title": doc.original_filename,
        "fileType": doc.file_type,
        "fileSizeKb": doc.file_size // 1024,
        "status": doc.status,
        "chunkCount": doc.chunk_count,
        "tags": doc.tags or ["auto-indexed"],
        "uploadedBy": current_user.full_name,
        "uploadedAt": doc.created_at.isoformat(),
        "version": doc.version,
        "ocrApplied": ext == "pdf",
        "folderPath": doc.folder_path,
    }

@router.delete("/{id}")
async def delete_document(
    id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
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
        delete_document_vectors(doc.id)
    except Exception as e:
        print(f"Failed to delete Qdrant vectors: {e}")
        
    # Clean file locally
    if os.path.exists(doc.file_path):
        try:
            os.remove(doc.file_path)
        except Exception as e:
            print(f"Failed to remove local file: {e}")
            
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
