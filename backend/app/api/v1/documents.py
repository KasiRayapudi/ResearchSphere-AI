from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User
from app.models.workspace import Workspace
from app.models.document import Document, DocumentChunk
from app.rag.document_processor import extract_text, clean_text, chunk_text
from app.rag.embeddings import embed_texts
from app.rag.vector_store import upsert_chunks, delete_document_vectors
from app.core.config import settings
from app.core.audit import audit, AuditAction, AuditOutcome
import os
import shutil
from datetime import datetime

router = APIRouter()

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
    workspace_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Find active workspace if not specified
    if not workspace_id:
        ws = db.query(Workspace).filter(Workspace.owner_id == current_user.id).first()
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
    if not workspace_id:
        ws = db.query(Workspace).filter(Workspace.owner_id == current_user.id).first()
        if not ws:
            raise HTTPException(status_code=400, detail="Create a workspace first.")
        workspace_id = ws.id

    # 2. Check upload directory
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(settings.UPLOAD_DIR, f"{datetime.utcnow().timestamp()}_{file.filename}")
    
    # Save file locally
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        
    file_size = os.path.getsize(file_path)
    ext = file.filename.split(".")[-1].lower() if file.filename else "txt"
    
    # 3. Create Document DB entry
    doc = Document(
        workspace_id=workspace_id,
        uploaded_by=current_user.id,
        filename=os.path.basename(file_path),
        original_filename=file.filename or "unknown",
        file_type=ext,
        file_size=file_size,
        file_path=file_path,
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
        # Clean up file on failure
        if os.path.exists(file_path):
            os.remove(file_path)
        audit(
            action=AuditAction.DOCUMENT_UPLOAD,
            actor=current_user,
            outcome=AuditOutcome.FAILURE,
            resource=f"document:{doc.id}",
            request=request,
            metadata={
                "filename": doc.original_filename,
                "file_type": ext,
                "file_size": file_size,
                "workspace_id": workspace_id,
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
            "file_size": file_size,
            "chunk_count": doc.chunk_count,
            "workspace_id": workspace_id,
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
    doc = db.query(Document).filter(Document.id == id).first()
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
