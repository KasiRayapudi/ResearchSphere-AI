from typing import Any, TypedDict


class AgentState(TypedDict):
    research_objective: str
    subtasks: list[dict[str, Any]]
    collected_sources: list[dict[str, Any]]
    retrieved_chunks: list[dict[str, Any]]
    synthesized_summary: str | None
    critic_verified: bool
    confidence_score: float
    citations: list[dict[str, Any]]
    final_report: dict[str, Any] | None
    agent_trace: list[dict[str, Any]]
