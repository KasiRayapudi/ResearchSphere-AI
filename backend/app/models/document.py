import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class DocumentStatus(str):
    #: Accepted and stored, waiting for a worker to pick it up.
    QUEUED = "queued"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    #: Retained for rows written before ingestion became asynchronous. Nothing
    #: sets it any more; it is treated as a terminal unknown by the API.
    PENDING = "pending"

    #: States a document will not move on from without a new request.
    TERMINAL = ("indexed", "failed")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # Every document list filters by workspace and orders by created_at.
        # A composite index serves both in one seek; separate single-column
        # indexes would make the database sort the workspace's rows itself.
        Index("ix_documents_workspace_created", "workspace_id", "created_at"),
        # Status filtering within a workspace, used by the upload UI and by
        # the stalled-document reaper.
        Index("ix_documents_workspace_status", "workspace_id", "status"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_by = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # File info
    filename = Column(String(500), nullable=False)
    original_filename = Column(String(500), nullable=False)
    file_type = Column(String(20), nullable=False)  # pdf, docx, txt, md
    file_size = Column(Integer, nullable=False)  # bytes
    #: Legacy absolute path, written before storage was abstracted. Kept so
    #: existing rows stay readable and so a rollback has something to fall
    #: back on, but nothing writes it any more: an absolute path is host
    #: state, it leaks the server's directory layout, and it means nothing to
    #: a second replica or to an object store. New rows carry a key instead.
    file_path = Column(String(1000), nullable=True)

    #: Which backend holds the bytes. Recorded per row rather than read from
    #: configuration so a document uploaded before a provider switch can
    #: still be located afterwards.
    storage_provider = Column(String(20), nullable=True)
    #: Opaque object key, e.g. ``documents/<workspace>/<uuid>.pdf``. Never a
    #: path, never derived from the client's filename, and never returned to
    #: a client -- downloads go through the API, which authorises first.
    storage_key = Column(String(512), nullable=True, index=True)
    #: Backend-reported extras (etag, content type at rest). Diagnostic only.
    storage_metadata = Column(JSON, nullable=True)
    # SHA-256 of the file content: duplicate detection and integrity checks.
    # Indexed but not unique - the same content may legitimately exist in
    # different workspaces; uniqueness is enforced per workspace in the query.
    content_hash = Column(String(64), nullable=True, index=True)
    mime_type = Column(String(120), nullable=True)

    # Processing status. See DocumentStatus for the vocabulary.
    status = Column(String(20), default="queued", index=True)
    chunk_count = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)

    #: Coarse percentage for the upload UI, 0-100. Written at stage
    #: boundaries by the ingestion task rather than continuously, so it is
    #: cheap: a handful of UPDATEs per document.
    progress = Column(Integer, default=0, nullable=False)

    #: When a worker claimed the document and when it let go. Both NULL while
    #: queued. The pair is what makes a stalled document identifiable: a row
    #: that has been "processing" since long ago has lost its worker.
    processing_started_at = Column(DateTime, nullable=True)
    processing_completed_at = Column(DateTime, nullable=True)

    # Metadata
    version = Column(Integer, default=1)
    ocr_applied = Column(Boolean, default=False)
    folder_path = Column(String(500), default="/Uploads")
    title = Column(String(500), nullable=True)
    description = Column(Text, nullable=True)
    tags = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    indexed_at = Column(DateTime, nullable=True)

    # Relationships
    workspace = relationship("Workspace", back_populates="documents")
    uploaded_by_user = relationship("User", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        # Chunks are read and deleted per document, in chunk order. Without
        # this every ingest re-scans the whole table to clear a retry.
        Index("ix_document_chunks_document_index", "document_id", "chunk_index"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id = Column(String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)

    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    token_count = Column(Integer, default=0)

    # Qdrant point id for vector lookup
    qdrant_point_id = Column(String(36), nullable=True)

    # Metadata for citation
    page_number = Column(Integer, nullable=True)
    section_header = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    document = relationship("Document", back_populates="chunks")
