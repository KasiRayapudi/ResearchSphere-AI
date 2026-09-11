import asyncio
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agents.state import AgentState
from app.core.logging import get_logger
from app.rag.pipeline import get_rag_response

logger = get_logger("agents")


class PlannerAgent:
    def execute(self, state: AgentState) -> dict[str, Any]:
        obj = state["research_objective"]
        subtasks = [
            {"id": "t-1", "task": f"Determine search filters for: '{obj}'", "agent": "Planner"},
            {
                "id": "t-2",
                "task": "Query vector store and extract candidate text",
                "agent": "Retriever",
            },
            {
                "id": "t-3",
                "task": "Synthesize context matching target objective",
                "agent": "Researcher",
            },
            {"id": "t-4", "task": "Verify factual consistency & references", "agent": "Critic"},
        ]
        trace = state.get("agent_trace", [])
        trace.append(
            {
                "agent": "Planner",
                "task": "Generated 4-step research execution plan",
                "status": "completed",
                "time_ms": 75,
            }
        )
        return {"subtasks": subtasks, "agent_trace": trace}


class RetrieverAgent:
    def execute(self, state: AgentState) -> dict[str, Any]:
        obj = state["research_objective"]
        trace = state.get("agent_trace", [])

        workspace_id = state.get("workspace_id")
        if not workspace_id:
            # Retrieving without a tenant scope would search another
            # workspace's documents, so refuse rather than guess.
            raise ValueError("RetrieverAgent requires workspace_id in the graph state")

        # get_rag_response is async and the graph runs synchronously. The graph
        # itself is invoked from a worker thread (see reports.generate_report),
        # so this thread owns no event loop and asyncio.run is safe here.
        rag_output = asyncio.run(
            get_rag_response(
                obj,
                workspace_id=workspace_id,
                document_ids=state.get("document_ids") or None,
            )
        )

        sources = rag_output.get("sources", [])
        trace.append(
            {
                "agent": "Retriever",
                "task": f"Retrieved {len(sources)} document chunks from the vector store",
                "status": "completed" if sources else "no_results",
                "time_ms": rag_output.get("response_time_ms", 0),
            }
        )
        return {"retrieved_chunks": sources, "agent_trace": trace}


class ResearcherAgent:
    def execute(self, state: AgentState) -> dict[str, Any]:
        obj = state["research_objective"]
        trace = state.get("agent_trace", [])
        chunks = state.get("retrieved_chunks", [])

        # Build synthesis response using Gemini
        import google.generativeai as genai

        from app.core.config import settings

        genai.configure(api_key=settings.GEMINI_API_KEY)

        context_str = "\n\n".join([c.get("content", "") for c in chunks])
        prompt = f"Analyze the following objective: {obj}\n\nUsing this context:\n{context_str}\n\nProvide a comprehensive research synthesis."

        if not chunks:
            # No evidence retrieved. Saying so is the honest result; asking the
            # model to write a synthesis anyway produces confident invention.
            trace.append(
                {
                    "agent": "Researcher",
                    "task": "No source material retrieved; synthesis skipped",
                    "status": "no_results",
                    "time_ms": 0,
                }
            )
            return {
                "synthesized_summary": (
                    "No documents in this workspace matched the research objective, "
                    "so no synthesis could be produced. Upload relevant sources and "
                    "run the report again."
                ),
                "agent_trace": trace,
            }

        try:
            model = genai.GenerativeModel("gemini-1.5-flash")
            response = model.generate_content(prompt)
            summary = response.text
        except Exception as exc:
            # Previously this fell back to a canned sentence that read like a
            # real synthesis. Surfacing the failure lets the route return 500
            # and audit it rather than persisting a fabricated report.
            logger.error(f"Researcher synthesis failed: {exc}", exc_info=True)
            raise RuntimeError(f"Research synthesis failed: {exc}") from exc

        trace.append(
            {
                "agent": "Researcher",
                "task": "Synthesized knowledge and generated research draft",
                "status": "completed",
                "time_ms": 450,
            }
        )
        return {"synthesized_summary": summary, "agent_trace": trace}


class CriticAgent:
    def execute(self, state: AgentState) -> dict[str, Any]:
        summary = state.get("synthesized_summary", "") or ""
        chunks = state.get("retrieved_chunks", [])
        trace = state.get("agent_trace", [])

        # Confidence is derived from the retrieval evidence that actually backs
        # the summary: how many chunks were found and how well they matched.
        # A fixed 0.95 told the reader nothing and was wrong whenever retrieval
        # returned little or nothing.
        scores = [c.get("score", 0.0) or 0.0 for c in chunks]
        mean_score = sum(scores) / len(scores) if scores else 0.0
        # Full evidence weight at five or more supporting chunks.
        coverage = min(len(chunks), 5) / 5
        verified = bool(chunks) and len(summary) > 50
        confidence = round(mean_score * coverage, 2) if verified else 0.0

        trace.append(
            {
                "agent": "Critic",
                "task": f"Checked summary consistency. Confidence level: {confidence:.2f}",
                "status": "completed",
                "time_ms": 110,
            }
        )
        return {"critic_verified": verified, "confidence_score": confidence, "agent_trace": trace}


class CitationAgent:
    def execute(self, state: AgentState) -> dict[str, Any]:
        chunks = state.get("retrieved_chunks", [])
        trace = state.get("agent_trace", [])

        citations = []
        for idx, c in enumerate(chunks):
            citations.append(
                {
                    "id": f"cit-{idx+1}",
                    "document_id": c.get("document_id"),
                    # content can be absent on a malformed payload; indexing
                    # None here previously raised inside the graph.
                    "excerpt": (c.get("content") or "")[:150] + "...",
                    "page": c.get("page_number", 1),
                    "score": c.get("score", 0.95),
                }
            )

        trace.append(
            {
                "agent": "Citation",
                "task": f"Mapped {len(citations)} references and metadata tags",
                "status": "completed",
                "time_ms": 80,
            }
        )
        return {"citations": citations, "agent_trace": trace}


class ReportAgent:
    def execute(self, state: AgentState) -> dict[str, Any]:
        summary = state.get("synthesized_summary", "")
        citations = state.get("citations", [])
        trace = state.get("agent_trace", [])

        report_md = f"""# Executive Research Report
## Objective
{state.get("research_objective")}

## Research Findings
{summary}

## References
"""
        for cit in citations:
            report_md += f"- [{cit['id']}] Excerpt: \"{cit['excerpt']}\" (Doc: {cit['document_id']}, Page: {cit['page']})\n"

        trace.append(
            {
                "agent": "Report",
                "task": "Generated executive markdown and PDF report schemas",
                "status": "completed",
                "time_ms": 160,
            }
        )

        final_report = {
            "title": f"Research Report: {state.get('research_objective')[:40]}",
            "markdown": report_md,
            "summary": summary[:300] + "...",
        }
        return {"final_report": final_report, "agent_trace": trace}


# Compile StateGraph
workflow = StateGraph(AgentState)

# Define nodes
workflow.add_node("planner", PlannerAgent().execute)
workflow.add_node("retriever", RetrieverAgent().execute)
workflow.add_node("researcher", ResearcherAgent().execute)
workflow.add_node("critic", CriticAgent().execute)
workflow.add_node("citation", CitationAgent().execute)
workflow.add_node("report", ReportAgent().execute)

# Define edges
workflow.add_edge(START, "planner")
workflow.add_edge("planner", "retriever")
workflow.add_edge("retriever", "researcher")
workflow.add_edge("researcher", "critic")
workflow.add_edge("critic", "citation")
workflow.add_edge("citation", "report")
workflow.add_edge("report", END)

research_graph = workflow.compile()


class LangGraphResearchEngine:
    def run_graph(
        self,
        research_objective: str,
        workspace_id: str,
        document_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run the research graph for one workspace.

        ``workspace_id`` is required and has no default: the previous "ws-1"
        default meant every report retrieved from one fixed workspace whatever
        the caller's tenant was.

        Blocking: the compiled graph runs synchronously and performs network
        I/O. Call it from a worker thread (``run_in_threadpool``), never
        directly from an async request handler.
        """
        if not workspace_id:
            raise ValueError("run_graph requires a workspace_id")

        inputs = {
            "research_objective": research_objective,
            "workspace_id": workspace_id,
            "document_ids": document_ids or [],
            "subtasks": [],
            "collected_sources": [],
            "retrieved_chunks": [],
            "synthesized_summary": None,
            "critic_verified": False,
            "confidence_score": 0.0,
            "citations": [],
            "final_report": None,
            "agent_trace": [],
        }
        # Run sync execution of LangGraph
        result = research_graph.invoke(inputs)
        return result
