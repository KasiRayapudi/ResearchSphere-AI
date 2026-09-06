import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Integer, Float, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.core.database import Base


class DocumentStatus(str):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


class Document(Base):
    __tablename__ = "documents"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    uploaded_by = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    
    # File info
    filename = Column(String(500), nullable=False)
    original_filename = Column(String(500), nullable=False)
    file_type = Column(String(20), nullable=False)  # pdf, docx, txt, md
    file_size = Column(Integer, nullable=False)  # bytes
    file_path = Column(String(1000), nullable=False)
    
    # Processing status
    status = Column(String(20), default="pending")  # pending, processing, indexed, failed
    chunk_count = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    
    # Metadata
    version = Column(Integer, default=1)
    ocr_applied = Column(Boolean, default=False)
    folder_path = Column(String(500), default="/Uploads")
    # Existing fields continue unchanged
    title = Column(String(500), nullable=True)
    description = Column(Text, nullable=True)
    tags = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    indexed_at = Column(DateTime, nullable=True)
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
