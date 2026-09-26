import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.database import Base


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        # Reports and research sessions are both listed per workspace, per
        # user, newest first.
        Index("ix_reports_workspace_user_created", "workspace_id", "user_id", "created_at"),
    )

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

    #: The agent trace this report was actually produced by, as recorded by the
    #: LangGraph run. Persisted so the research view can show what really ran;
    #: it previously rendered a fixed four-step script with invented timings.
    agent_trace = Column(JSON, default=list)

    #: Confidence reported by the critic agent for this run.
    confidence_score = Column(Float, nullable=True)

    format = Column(String(20), default="markdown")  # markdown, pdf
    status = Column(String(20), default="generating")  # generating, ready, failed

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    workspace = relationship("Workspace", back_populates="reports")
    user = relationship("User", back_populates="reports")


class Connector(Base):
    __tablename__ = "connectors"
    __table_args__ = (Index("ix_connectors_workspace", "workspace_id"),)

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
