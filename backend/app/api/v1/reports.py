from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.graph import LangGraphResearchEngine
from app.core import metrics
from app.core.audit import AuditAction, AuditOutcome, audit
from app.core.database import get_db
from app.core.security import get_current_user, resolve_workspace
from app.models.report import Report as ReportModel
from app.models.user import User

router = APIRouter()
engine = LangGraphResearchEngine()


class ReportGenerateRequest(BaseModel):
    title: str
    objective: str
    workspace_id: str | None = None
    document_ids: list[str] = []


@router.get("")
async def get_reports(
    request: Request,
    workspace_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        return []
    workspace_id = ws.id

    reports = (
        db.query(ReportModel)
        .filter(ReportModel.workspace_id == workspace_id, ReportModel.user_id == current_user.id)
        .order_by(ReportModel.created_at.desc())
        .all()
    )

    return [
        {
            "id": r.id,
            "title": r.title,
            "format": r.format,
            "summary": r.executive_summary or "Executive Research synthesis",
            "objective": r.query,
            "generatedAt": r.created_at.isoformat(),
            "author": current_user.full_name,
            "sourceDocumentIds": r.source_document_ids or [],
            "sections": [
                {"title": "Executive Summary", "content": r.executive_summary or ""},
                {"title": "Research Findings & Analysis", "content": r.findings or ""},
                {
                    "title": "Technical Assessment & Limitation",
                    "content": r.technical_analysis or "",
                },
            ],
        }
        for r in reports
    ]


@router.post("/generate")
async def generate_report(
    payload: ReportGenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = resolve_workspace(payload.workspace_id, request, db, current_user)
    if not ws:
        raise HTTPException(status_code=400, detail="Workspace required")
    workspace_id = ws.id

    # 1. Run RAG and LangGraph agent workflow to synthesize the report contents.
    #
    # run_graph is synchronous and does network I/O (vector search, Gemini).
    # Calling it directly from this async handler blocked the event loop for
    # the whole run and, because the retriever needs its own loop, made
    # retrieval raise on every request. Offloading to a worker thread fixes
    # both: other requests keep being served and the retriever works.
    try:
        graph_output = await run_in_threadpool(
            engine.run_graph,
            payload.objective,
            workspace_id=workspace_id,
            document_ids=payload.document_ids or None,
        )
    except Exception as e:
        audit(
            action=AuditAction.REPORT_GENERATE,
            actor=current_user,
            outcome=AuditOutcome.FAILURE,
            resource="report:new",
            request=request,
            metadata={
                "title": payload.title,
                "workspace_id": workspace_id,
                "reason": str(e)[:200],
            },
        )
        metrics.safe(metrics.reports_generated_total.labels(outcome="failed").inc)
        raise HTTPException(
            status_code=500, detail=f"LangGraph failed to generate report: {str(e)}"
        ) from e

    final_report_data = graph_output.get("final_report") or {}

    # 2. Write to database
    report = ReportModel(
        workspace_id=workspace_id,
        user_id=current_user.id,
        title=payload.title,
        query=payload.objective,
        executive_summary=graph_output.get("synthesized_summary"),
        findings=final_report_data.get("markdown"),
        # Report what the critic actually concluded. The previous text asserted
        # "Fact verified" unconditionally and defaulted the score to 0.95, so a
        # run with no evidence still claimed high confidence.
        technical_analysis=(
            (
                "Critic agent verified the synthesis against retrieved sources. "
                if graph_output.get("critic_verified")
                else "Critic agent could not verify the synthesis against retrieved sources. "
            )
            + f"Confidence level: {graph_output.get('confidence_score', 0.0)}. "
            + f"Supporting chunks retrieved: {len(graph_output.get('retrieved_chunks') or [])}."
        ),
        references=graph_output.get("citations", []),
        content_markdown=final_report_data.get("markdown"),
        source_document_ids=payload.document_ids
        or [
            c.get("document_id") for c in graph_output.get("citations", []) if c.get("document_id")
        ],
        agent_trace=graph_output.get("agent_trace", []),
        confidence_score=graph_output.get("confidence_score", 0.0),
        format="pdf",
        status="ready",
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    metrics.safe(metrics.reports_generated_total.labels(outcome="success").inc)
    audit(
        action=AuditAction.REPORT_GENERATE,
        actor=current_user,
        outcome=AuditOutcome.SUCCESS,
        resource=f"report:{report.id}",
        request=request,
        metadata={
            "title": report.title,
            "workspace_id": workspace_id,
            "source_document_count": len(report.source_document_ids or []),
        },
    )

    return {
        "id": report.id,
        "title": report.title,
        "format": report.format,
        "summary": report.executive_summary,
        "objective": report.query,
        "generatedAt": report.created_at.isoformat(),
        "author": current_user.full_name,
        "sourceDocumentIds": report.source_document_ids,
        "sections": [
            {"title": "Executive Summary", "content": report.executive_summary},
            {"title": "Research Findings & Analysis", "content": report.findings},
            {"title": "Technical Assessment & Limitation", "content": report.technical_analysis},
        ],
    }
