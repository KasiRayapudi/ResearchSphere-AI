from typing import Any, TypedDict


class AgentState(TypedDict):
    research_objective: str
    #: Tenant the retrieval step must be scoped to. Required: without it
    #: the retriever has no way to know whose documents it may read.
    workspace_id: str
    #: Optional narrowing to specific documents within that workspace.
    document_ids: list[str]
    subtasks: list[dict[str, Any]]
    collected_sources: list[dict[str, Any]]
    retrieved_chunks: list[dict[str, Any]]
    synthesized_summary: str | None
    critic_verified: bool
    confidence_score: float
    citations: list[dict[str, Any]]
    final_report: dict[str, Any] | None
    agent_trace: list[dict[str, Any]]
