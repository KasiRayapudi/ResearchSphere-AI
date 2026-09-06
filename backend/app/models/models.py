import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.core.database import Base


class UserModel(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String(50), default="researcher")  # owner, admin, researcher, viewer
    avatar_url = Column(String, nullable=True)
    storage_limit_mb = Column(Integer, default=50000)
    storage_used_mb = Column(Integer, default=12840)
    created_at = Column(DateTime, default=datetime.utcnow)


class WorkspaceModel(Base):
    __tablename__ = "workspaces"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, index=True, nullable=False)
    icon = Column(String(50), default="Sparkles")
    member_count = Column(Integer, default=1)
    document_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class DocumentModel(Base):
    __tablename__ = "documents"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=True)
    title = Column(String(255), nullable=False)
    file_type = Column(String(20), nullable=False)
    file_size_kb = Column(Integer, default=0)
    status = Column(String(50), default="indexed")  # indexed, chunking, embedding, failed
    chunk_count = Column(Integer, default=0)
    tags = Column(JSON, default=list)
    uploaded_by = Column(String(255), nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    version = Column(Integer, default=1)
    ocr_applied = Column(Boolean, default=False)
    folder_path = Column(String(255), default="/Uploads")


class DocumentChunkModel(Base):
    __tablename__ = "document_chunks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id = Column(String, ForeignKey("documents.id"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    qdrant_point_id = Column(String, nullable=True)
    page_number = Column(Integer, nullable=True)
    confidence_score = Column(Float, default=0.95)
    created_at = Column(DateTime, default=datetime.utcnow)


class ResearchSessionModel(Base):
    __tablename__ = "research_sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(255), nullable=False)
    objective = Column(Text, nullable=False)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=True)
    status = Column(String(50), default="in_progress")
    progress_percentage = Column(Integer, default=25)
    sources_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class ChatMessageModel(Base):
    __tablename__ = "chat_messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String, ForeignKey("research_sessions.id"), nullable=True)
    role = Column(String(20), nullable=False)  # user, assistant, system
    content = Column(Text, nullable=False)
    citations = Column(JSON, default=list)
    agent_steps = Column(JSON, default=list)
    model_used = Column(String(100), default="Gemini 1.5 Pro")
    timestamp = Column(DateTime, default=datetime.utcnow)


class ReportModel(Base):
    __tablename__ = "reports"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(255), nullable=False)
    format = Column(String(20), default="pdf")
    summary = Column(Text, nullable=False)
    objective = Column(Text, nullable=False)
    sections = Column(JSON, default=list)
    author = Column(String(255), nullable=False)
    generated_at = Column(DateTime, default=datetime.utcnow)


class MCPConnectorModel(Base):
    __tablename__ = "mcp_connectors"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    provider = Column(String(50), nullable=False)  # github, gdrive, local, slack, notion, etc.
    status = Column(String(50), default="connected")
    last_synced_at = Column(DateTime, default=datetime.utcnow)
    items_synced_count = Column(Integer, default=0)
    config = Column(JSON, default=dict)
    is_future_connector = Column(Boolean, default=False)
