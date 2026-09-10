from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.graph import LangGraphResearchEngine
from app.core import metrics
from app.core.database import get_db
from app.core.security import get_current_user, resolve_workspace
from app.core.workspace_access import require_workspace_role
from app.models.report import Report as ReportModel
from app.models.user import User

router = APIRouter()
engine = LangGraphResearchEngine()


class StartResearchRequest(BaseModel):
    title: str
    objective: str
    workspace_id: str | None = None


@router.get("")
async def get_sessions(
    request: Request,
    workspace_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = resolve_workspace(workspace_id, request, db, current_user)
    if not ws:
        return []
    workspace_id = ws.id

    # For MVP, we can return generated research sessions mapped to Reports or ChatSessions
    # Or query from DB. Let's fetch from Report table since report represents a compiled agent research session!
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
            "objective": r.query,
            "workspaceId": r.workspace_id,
            "status": r.status,
            "progressPercentage": 100 if r.status == "ready" else 0,
            "sourcesCount": len(r.source_document_ids or []),
            "createdAt": r.created_at.isoformat(),
            "updatedAt": r.updated_at.isoformat(),
            # The trace recorded by the run that produced this report. Empty
            # for reports generated before the trace was persisted -- an empty
            # list is honest; the fixed four-step script that used to be
            # returned here was not.
            "agentSteps": [
                {
                    "id": f"as-{idx}",
                    "agentName": step.get("agent"),
                    "status": step.get("status"),
                    "task": step.get("task"),
                    "executionTimeMs": step.get("time_ms"),
                    "timestamp": r.created_at.isoformat(),
                }
                for idx, step in enumerate(r.agent_trace or [])
            ],
        }
        for r in reports
    ]


@router.post("/start")
async def start_session(
    request: Request,
    payload: StartResearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ws = resolve_workspace(payload.workspace_id, request, db, current_user)
    if not ws:
        raise HTTPException(status_code=400, detail="Workspace required")
    # Same as reports: this writes a record and runs the agent graph.
    require_workspace_role(request, ws, "content.write", current_user, db=db)
    workspace_id = ws.id

    # 1. Run multi-agent LangGraph workflow.
    #
    # run_graph is synchronous and does network I/O; see reports.generate_report
    # for why it must not be called inline from an async handler.
    try:
        graph_output = await run_in_threadpool(
            engine.run_graph, payload.objective, workspace_id=workspace_id
        )
    except Exception as e:
        metrics.safe(metrics.research_sessions_total.labels(outcome="failed").inc)
        raise HTTPException(status_code=500, detail=f"LangGraph execution failed: {str(e)}") from e

    final_report_data = graph_output.get("final_report") or {}

    # 2. Save final synthesized report in database
    report = ReportModel(
        workspace_id=workspace_id,
        user_id=current_user.id,
        title=payload.title,
        query=payload.objective,
        executive_summary=graph_output.get("synthesized_summary"),
        findings=final_report_data.get("markdown"),
        # Report what the critic concluded rather than asserting verification
        # and defaulting the score to 0.95.
        technical_analysis=(
            (
                "Critic agent verified the synthesis against retrieved sources. "
                if graph_output.get("critic_verified")
                else "Critic agent could not verify the synthesis against retrieved sources. "
            )
            + f"Confidence score: {graph_output.get('confidence_score', 0.0)}."
        ),
        agent_trace=graph_output.get("agent_trace", []),
        confidence_score=graph_output.get("confidence_score", 0.0),
        references=graph_output.get("citations", []),
        content_markdown=final_report_data.get("markdown"),
        source_document_ids=[
            c.get("document_id") for c in graph_output.get("citations", []) if c.get("document_id")
        ],
        format="markdown",
        status="ready",
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    metrics.safe(metrics.research_sessions_total.labels(outcome="success").inc)

    # 3. Return payload mapping visual agent graph steps
    return {
        "id": report.id,
        "title": report.title,
        "objective": report.query,
        "workspaceId": report.workspace_id,
        "status": report.status,
        "progressPercentage": 100,
        "sourcesCount": len(report.source_document_ids),
        "createdAt": report.created_at.isoformat(),
        "updatedAt": report.updated_at.isoformat(),
        "agentSteps": [
            {
                "id": f"as-{idx}",
                "agentName": trace.get("agent"),
                "status": trace.get("status"),
                "task": trace.get("task"),
                "executionTimeMs": trace.get("time_ms"),
                "timestamp": report.created_at.isoformat(),
            }
            for idx, trace in enumerate(graph_output.get("agent_trace", []))
        ],
    }
