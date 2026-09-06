import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class Report(Base):
    __tablename__ = "reports"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    title = Column(String(500), nullable=False)
    query = Column(Text, nullable=False)  # original research question

    # Report content (structured)
    executive_summary = Column(Text, nullable=True)
    findings = Column(Text, nullable=True)
    technical_analysis = Column(Text, nullable=True)
    references = Column(JSON, default=list)

    # Full markdown content
    content_markdown = Column(Text, nullable=True)

    # Source documents used
    source_document_ids = Column(JSON, default=list)

    format = Column(String(20), default="markdown")  # markdown, pdf
    status = Column(String(20), default="generating")  # generating, ready, failed

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    workspace = relationship("Workspace", back_populates="reports")
    user = relationship("User", back_populates="reports")


class Connector(Base):
    __tablename__ = "connectors"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id = Column(
        String(36), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )

    name = Column(String(255), nullable=False)
    connector_type = Column(String(50), nullable=False)  # local, github, gdrive
    config = Column(JSON, default=dict)  # connector-specific config

    status = Column(String(20), default="active")  # active, inactive, error
    last_synced_at = Column(DateTime, nullable=True)
    documents_synced = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    workspace = relationship("Workspace", back_populates="connectors")
