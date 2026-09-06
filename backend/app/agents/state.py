from typing import List, Dict, Any, TypedDict, Optional

class AgentState(TypedDict):
    research_objective: str
    subtasks: List[Dict[str, Any]]
    collected_sources: List[Dict[str, Any]]
    retrieved_chunks: List[Dict[str, Any]]
    synthesized_summary: Optional[str]
    critic_verified: bool
    confidence_score: float
    citations: List[Dict[str, Any]]
    final_report: Optional[Dict[str, Any]]
    agent_trace: List[Dict[str, Any]]
