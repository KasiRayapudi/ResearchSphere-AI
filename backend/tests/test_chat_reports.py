"""
Chat streaming and report generation.

The RAG pipeline and the LangGraph engine are substituted at their module
boundaries; everything else -- session ownership, SSE framing, persistence,
tenant isolation and failure handling -- is the real code path.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.integration


def _sse_events(response) -> list[dict]:
    """Decode an SSE body into the JSON payloads it carried."""
    events = []
    for line in response.text.splitlines():
        if not line.startswith("data: "):
            continue
        body = line[len("data: ") :]
        if body == "[DONE]":
            events.append({"__done__": True})
            continue
        events.append(json.loads(body))
    return events


def _stream_of(*chunks):
    """Build a stand-in for stream_rag_response yielding the given chunks."""

    async def _generator(question, workspace_id):
        for chunk in chunks:
            yield chunk

    return _generator


SOURCES_FRAME = (
    "__SOURCES_JSON__"
    + json.dumps(
        {
            "__sources__": [
                {"document_id": "doc-1", "chunk_id": "c1", "content": "ctx", "score": 0.9}
            ],
            "__response_time_ms__": 42,
        }
    )
    + "__END_SOURCES__"
)


# -------------------------------------------------------------- chat sessions --
class TestChatSessions:
    def test_listing_requires_authentication(self, client):
        assert client.get("/api/v1/chat/sessions").status_code == 401

    def test_new_account_has_no_sessions(self, client, auth_headers):
        response = client.get("/api/v1/chat/sessions", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []

    def test_session_can_be_created_and_listed(self, client, auth_headers, workspace_id):
        created = client.post(
            f"/api/v1/chat/sessions?workspace_id={workspace_id}", headers=auth_headers
        )
        assert created.status_code == 200
        session_id = created.json()["id"]

        listed = client.get("/api/v1/chat/sessions", headers=auth_headers).json()
        assert any(item["id"] == session_id for item in listed)

    def test_listed_session_uses_the_frontend_field_names(
        self, client, auth_headers, workspace_id
    ):
        client.post(f"/api/v1/chat/sessions?workspace_id={workspace_id}", headers=auth_headers)
        item = client.get("/api/v1/chat/sessions", headers=auth_headers).json()[0]
        # Renaming any of these silently breaks the chat sidebar.
        assert {"id", "title", "workspaceId", "createdAt"} <= set(item)

    def test_creating_in_another_users_workspace_is_refused(
        self, client, auth_headers, workspace_id
    ):
        from tests.conftest import STRONG_PASSWORD

        other = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Other",
                "email": "chat_other_ws@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if other.status_code != 200:  # already created by an earlier run in-session
            other = client.post(
                "/api/v1/auth/login",
                json={"email": "chat_other_ws@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

        response = client.post(
            f"/api/v1/chat/sessions?workspace_id={workspace_id}", headers=headers
        )
        # resolve_workspace must not hand over a workspace the caller does not own.
        assert response.status_code in (400, 403, 404)

    def test_sessions_are_not_visible_across_accounts(self, client, auth_headers, workspace_id):
        from tests.conftest import STRONG_PASSWORD

        client.post(f"/api/v1/chat/sessions?workspace_id={workspace_id}", headers=auth_headers)

        signup = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Second",
                "email": "chat_isolation@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if signup.status_code != 200:
            signup = client.post(
                "/api/v1/auth/login",
                json={"email": "chat_isolation@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

        assert client.get("/api/v1/chat/sessions", headers=headers).json() == []


# --------------------------------------------------------------- chat stream --
class TestChatStream:
    def test_streaming_requires_authentication(self, client):
        assert client.post("/api/v1/chat/stream", json={"prompt": "hi"}).status_code == 401

    def test_tokens_are_streamed_as_sse_and_terminated(self, client, auth_headers, workspace_id):
        with patch(
            "app.api.v1.chat.stream_rag_response",
            _stream_of("Hello ", "world", SOURCES_FRAME),
        ):
            response = client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={"prompt": "Question?", "workspace_id": workspace_id},
            )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(response)
        assert [e.get("text") for e in events if "text" in e] == ["Hello ", "world"]
        assert events[-1] == {"__done__": True}

    def test_answer_is_persisted_with_its_sources(self, client, auth_headers, workspace_id):
        with patch(
            "app.api.v1.chat.stream_rag_response", _stream_of("Answer text", SOURCES_FRAME)
        ):
            client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={"prompt": "Persisted?", "workspace_id": workspace_id},
            )

        from app.core.database import SessionLocal
        from app.models.chat import ChatMessage

        session = SessionLocal()
        try:
            message = (
                session.query(ChatMessage)
                .filter(ChatMessage.role == "assistant", ChatMessage.content == "Answer text")
                .first()
            )
            assert message is not None
            assert message.sources[0]["document_id"] == "doc-1"
            assert message.response_time_ms == 42
        finally:
            session.close()

    def test_user_message_is_persisted_before_the_stream(
        self, client, auth_headers, workspace_id
    ):
        with patch("app.api.v1.chat.stream_rag_response", _stream_of("x", SOURCES_FRAME)):
            client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={"prompt": "unique-user-prompt-9271", "workspace_id": workspace_id},
            )

        from app.core.database import SessionLocal
        from app.models.chat import ChatMessage

        session = SessionLocal()
        try:
            assert (
                session.query(ChatMessage)
                .filter(ChatMessage.content == "unique-user-prompt-9271")
                .first()
                is not None
            )
        finally:
            session.close()

    def test_a_session_is_created_when_none_is_supplied(
        self, client, auth_headers, workspace_id
    ):
        assert client.get("/api/v1/chat/sessions", headers=auth_headers).json() == []

        with patch("app.api.v1.chat.stream_rag_response", _stream_of("x", SOURCES_FRAME)):
            client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={"prompt": "A brand new conversation", "workspace_id": workspace_id},
            )

        sessions = client.get("/api/v1/chat/sessions", headers=auth_headers).json()
        assert len(sessions) == 1
        # The first prompt names the session, so the sidebar is not all "New Chat".
        assert sessions[0]["title"] == "A brand new conversation"[:40]

    def test_another_users_session_id_is_rejected(self, client, auth_headers, workspace_id):
        from tests.conftest import STRONG_PASSWORD

        created = client.post(
            f"/api/v1/chat/sessions?workspace_id={workspace_id}", headers=auth_headers
        )
        victim_session = created.json()["id"]

        signup = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Attacker",
                "email": "chat_attacker@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if signup.status_code != 200:
            signup = client.post(
                "/api/v1/auth/login",
                json={"email": "chat_attacker@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

        with patch("app.api.v1.chat.stream_rag_response", _stream_of("x", SOURCES_FRAME)):
            response = client.post(
                "/api/v1/chat/stream",
                headers=headers,
                json={"prompt": "steal", "session_id": victim_session},
            )

        # IDOR: writing into someone else's conversation must 404, not succeed.
        assert response.status_code == 404

    def test_unknown_session_id_is_rejected(self, client, auth_headers, workspace_id):
        with patch("app.api.v1.chat.stream_rag_response", _stream_of("x", SOURCES_FRAME)):
            response = client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={
                    "prompt": "q",
                    "workspace_id": workspace_id,
                    "session_id": "does-not-exist",
                },
            )
        assert response.status_code == 404

    def test_pipeline_failure_yields_an_error_event_not_a_truncated_stream(
        self, client, auth_headers, workspace_id
    ):
        async def _failing(question, workspace_id):
            yield "partial"
            raise RuntimeError("Gemini exploded")

        with patch("app.api.v1.chat.stream_rag_response", _failing):
            response = client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={"prompt": "q", "workspace_id": workspace_id},
            )

        # Status is already 200 by the time the failure happens, so the contract
        # is a typed error frame followed by a normal terminator.
        assert response.status_code == 200
        events = _sse_events(response)
        assert any("error" in e for e in events)
        assert events[-1] == {"__done__": True}
        # The failure reason must not leak to the browser.
        assert "Gemini exploded" not in response.text

    def test_malformed_metadata_frame_does_not_break_the_stream(
        self, client, auth_headers, workspace_id
    ):
        broken = "__SOURCES_JSON__{not valid json__END_SOURCES__"
        with patch("app.api.v1.chat.stream_rag_response", _stream_of("text", broken)):
            response = client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={"prompt": "q", "workspace_id": workspace_id},
            )

        events = _sse_events(response)
        assert {"text": "text"} in events
        assert events[-1] == {"__done__": True}

    def test_text_preceding_the_marker_is_not_dropped(self, client, auth_headers, workspace_id):
        combined = "trailing words" + SOURCES_FRAME
        with patch("app.api.v1.chat.stream_rag_response", _stream_of(combined)):
            response = client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={"prompt": "q", "workspace_id": workspace_id},
            )

        assert {"text": "trailing words"} in _sse_events(response)

    def test_time_to_first_token_is_recorded(self, client, auth_headers, workspace_id):
        with patch("app.api.v1.chat.stream_rag_response", _stream_of("tok", SOURCES_FRAME)):
            client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={"prompt": "q", "workspace_id": workspace_id},
            )
        body = client.get("/metrics").text
        assert "researchsphere_chat_time_to_first_token_seconds" in body


# ------------------------------------------------------------------ reports --
class TestReports:
    def test_listing_requires_authentication(self, client):
        assert client.get("/api/v1/reports").status_code == 401

    def test_new_account_has_no_reports(self, client, auth_headers):
        assert client.get("/api/v1/reports", headers=auth_headers).json() == []

    def _graph_output(self):
        return {
            "synthesized_summary": "The summary",
            "final_report": {"markdown": "# Findings\n\nDetail."},
            "citations": [{"document_id": "doc-7", "snippet": "cited text"}],
            "confidence_score": 0.88,
        }

    def test_report_is_generated_and_persisted(self, client, auth_headers, workspace_id):
        with patch.object(
            __import__("app.api.v1.reports", fromlist=["engine"]).engine,
            "run_graph",
            return_value=self._graph_output(),
        ):
            response = client.post(
                "/api/v1/reports/generate",
                headers=auth_headers,
                json={
                    "title": "Quarterly Analysis",
                    "objective": "Summarise the findings",
                    "workspace_id": workspace_id,
                },
            )

        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Quarterly Analysis"
        assert body["summary"] == "The summary"
        assert len(body["sections"]) == 3
        # Citations with a document id become the report's provenance.
        assert body["sourceDocumentIds"] == ["doc-7"]

        listed = client.get("/api/v1/reports", headers=auth_headers).json()
        assert any(item["id"] == body["id"] for item in listed)

    def test_explicit_document_ids_win_over_citation_ids(
        self, client, auth_headers, workspace_id
    ):
        with patch.object(
            __import__("app.api.v1.reports", fromlist=["engine"]).engine,
            "run_graph",
            return_value=self._graph_output(),
        ):
            response = client.post(
                "/api/v1/reports/generate",
                headers=auth_headers,
                json={
                    "title": "Scoped",
                    "objective": "obj",
                    "workspace_id": workspace_id,
                    "document_ids": ["chosen-1", "chosen-2"],
                },
            )
        assert response.json()["sourceDocumentIds"] == ["chosen-1", "chosen-2"]

    def test_confidence_is_recorded_in_the_technical_section(
        self, client, auth_headers, workspace_id
    ):
        with patch.object(
            __import__("app.api.v1.reports", fromlist=["engine"]).engine,
            "run_graph",
            return_value=self._graph_output(),
        ):
            response = client.post(
                "/api/v1/reports/generate",
                headers=auth_headers,
                json={"title": "T", "objective": "o", "workspace_id": workspace_id},
            )
        technical = response.json()["sections"][2]["content"]
        assert "0.88" in technical

    def test_graph_failure_returns_500_and_is_audited(self, client, auth_headers, workspace_id):
        engine = __import__("app.api.v1.reports", fromlist=["engine"]).engine
        with patch.object(engine, "run_graph", side_effect=RuntimeError("graph broke")):
            response = client.post(
                "/api/v1/reports/generate",
                headers=auth_headers,
                json={"title": "T", "objective": "o", "workspace_id": workspace_id},
            )

        assert response.status_code == 500
        samples = client.get("/metrics").text
        assert 'researchsphere_reports_generated_total{outcome="failed"}' in samples

    def test_empty_graph_output_still_produces_a_record(
        self, client, auth_headers, workspace_id
    ):
        engine = __import__("app.api.v1.reports", fromlist=["engine"]).engine
        with patch.object(engine, "run_graph", return_value={}):
            response = client.post(
                "/api/v1/reports/generate",
                headers=auth_headers,
                json={"title": "Sparse", "objective": "o", "workspace_id": workspace_id},
            )
        # A thin answer is not an error; the report exists with empty sections.
        assert response.status_code == 200
        assert response.json()["sourceDocumentIds"] == []

    def test_reports_are_not_visible_across_accounts(self, client, auth_headers, workspace_id):
        from tests.conftest import STRONG_PASSWORD

        engine = __import__("app.api.v1.reports", fromlist=["engine"]).engine
        with patch.object(engine, "run_graph", return_value=self._graph_output()):
            client.post(
                "/api/v1/reports/generate",
                headers=auth_headers,
                json={"title": "Private", "objective": "o", "workspace_id": workspace_id},
            )

        signup = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Nosy",
                "email": "report_isolation@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if signup.status_code != 200:
            signup = client.post(
                "/api/v1/auth/login",
                json={"email": "report_isolation@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

        assert client.get("/api/v1/reports", headers=headers).json() == []

    def test_generating_in_another_users_workspace_is_refused(
        self, client, auth_headers, workspace_id
    ):
        from tests.conftest import STRONG_PASSWORD

        signup = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Outsider",
                "email": "report_outsider@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if signup.status_code != 200:
            signup = client.post(
                "/api/v1/auth/login",
                json={"email": "report_outsider@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

        engine = __import__("app.api.v1.reports", fromlist=["engine"]).engine
        with patch.object(engine, "run_graph", return_value=self._graph_output()) as graph:
            response = client.post(
                "/api/v1/reports/generate",
                headers=headers,
                json={"title": "X", "objective": "o", "workspace_id": workspace_id},
            )

        assert response.status_code in (400, 403, 404)
        # The expensive agent run must be refused before it starts.
        graph.assert_not_called()

    def test_listed_report_uses_the_frontend_field_names(
        self, client, auth_headers, workspace_id
    ):
        engine = __import__("app.api.v1.reports", fromlist=["engine"]).engine
        with patch.object(engine, "run_graph", return_value=self._graph_output()):
            client.post(
                "/api/v1/reports/generate",
                headers=auth_headers,
                json={"title": "Shaped", "objective": "o", "workspace_id": workspace_id},
            )
        item = client.get("/api/v1/reports", headers=auth_headers).json()[0]
        assert {
            "id",
            "title",
            "format",
            "summary",
            "objective",
            "generatedAt",
            "author",
            "sourceDocumentIds",
            "sections",
        } <= set(item)


class TestLangGraphEngine:
    """The agent graph itself, with Gemini and the vector store substituted."""

    def test_run_graph_requires_a_workspace(self):
        from app.agents.graph import LangGraphResearchEngine

        # The old signature defaulted to "ws-1", so every tenant's report
        # retrieved from one fixed workspace. There is no safe default.
        with pytest.raises(ValueError, match="workspace_id"):
            LangGraphResearchEngine().run_graph("objective", workspace_id="")

    def test_retriever_scopes_search_to_the_callers_workspace(self):
        from app.agents import graph as graph_module

        seen = {}

        async def _capture(question, workspace_id, document_ids=None, top_k=5):
            seen["workspace_id"] = workspace_id
            seen["document_ids"] = document_ids
            return {"sources": [], "response_time_ms": 5}

        with patch.object(graph_module, "get_rag_response", _capture):
            graph_module.RetrieverAgent().execute(
                {
                    "research_objective": "obj",
                    "workspace_id": "ws-caller",
                    "document_ids": ["d1"],
                    "agent_trace": [],
                }
            )

        assert seen["workspace_id"] == "ws-caller"
        assert seen["document_ids"] == ["d1"]

    def test_retriever_refuses_to_run_unscoped(self):
        from app.agents import graph as graph_module

        with pytest.raises(ValueError, match="workspace_id"):
            graph_module.RetrieverAgent().execute(
                {"research_objective": "obj", "workspace_id": "", "agent_trace": []}
            )

    def test_retrieval_actually_runs_when_called_off_the_event_loop(
        self, client, auth_headers, workspace_id
    ):
        """Regression: retrieval used to raise on every request.

        run_graph was awaited inline in an async handler, so the retriever's
        run_until_complete hit "Cannot run the event loop while another loop is
        running", the bare except swallowed it, and every report was built from
        zero sources. The route now offloads to a worker thread.
        """
        from app.agents import graph as graph_module

        calls = []

        async def _sources(question, workspace_id, document_ids=None, top_k=5):
            calls.append(workspace_id)
            return {
                "sources": [
                    {"document_id": "doc-1", "content": "evidence text", "score": 0.9},
                    {"document_id": "doc-2", "content": "more evidence", "score": 0.7},
                ],
                "response_time_ms": 12,
            }

        fake_model = MagicMock()
        fake_model.generate_content.return_value = MagicMock(text="A grounded synthesis " * 5)

        with patch.object(graph_module, "get_rag_response", _sources), patch(
            "google.generativeai.GenerativeModel", return_value=fake_model
        ):
            response = client.post(
                "/api/v1/reports/generate",
                headers=auth_headers,
                json={"title": "Grounded", "objective": "obj", "workspace_id": workspace_id},
            )

        assert response.status_code == 200, response.text
        # Retrieval ran, and it ran against the caller's workspace.
        assert calls == [workspace_id]
        assert response.json()["sourceDocumentIds"] == ["doc-1", "doc-2"]

    def test_synthesis_failure_is_surfaced_not_papered_over(self):
        from app.agents import graph as graph_module

        with patch(
            "google.generativeai.GenerativeModel", side_effect=RuntimeError("no api key")
        ):
            with pytest.raises(RuntimeError, match="Research synthesis failed"):
                graph_module.ResearcherAgent().execute(
                    {
                        "research_objective": "obj",
                        "retrieved_chunks": [{"content": "evidence", "score": 0.9}],
                        "agent_trace": [],
                    }
                )

    def test_no_evidence_produces_an_honest_answer_not_an_invented_one(self):
        from app.agents import graph as graph_module

        result = graph_module.ResearcherAgent().execute(
            {"research_objective": "obj", "retrieved_chunks": [], "agent_trace": []}
        )
        summary = result["synthesized_summary"]
        assert "No documents in this workspace matched" in summary
        assert result["agent_trace"][-1]["status"] == "no_results"

    def test_confidence_reflects_the_evidence(self):
        from app.agents import graph as graph_module

        strong = graph_module.CriticAgent().execute(
            {
                "synthesized_summary": "s" * 100,
                "retrieved_chunks": [{"score": 0.95} for _ in range(5)],
                "agent_trace": [],
            }
        )
        weak = graph_module.CriticAgent().execute(
            {
                "synthesized_summary": "s" * 100,
                "retrieved_chunks": [{"score": 0.3}],
                "agent_trace": [],
            }
        )
        assert strong["confidence_score"] > weak["confidence_score"]
        assert strong["critic_verified"] is True

    def test_confidence_is_zero_without_evidence(self):
        from app.agents import graph as graph_module

        result = graph_module.CriticAgent().execute(
            {"synthesized_summary": "s" * 100, "retrieved_chunks": [], "agent_trace": []}
        )
        # A summary with nothing behind it must not report high confidence.
        assert result["confidence_score"] == 0.0
        assert result["critic_verified"] is False

    def test_citations_survive_a_chunk_with_no_content(self):
        from app.agents import graph as graph_module

        result = graph_module.CitationAgent().execute(
            {
                "retrieved_chunks": [{"document_id": "d1", "content": None, "score": 0.5}],
                "agent_trace": [],
            }
        )
        # Indexing None here used to raise inside the graph.
        assert result["citations"][0]["document_id"] == "d1"

    def test_planner_produces_the_full_step_plan(self):
        from app.agents import graph as graph_module

        result = graph_module.PlannerAgent().execute(
            {"research_objective": "obj", "agent_trace": []}
        )
        assert len(result["subtasks"]) == 4
        assert result["agent_trace"][-1]["agent"] == "Planner"

    def test_report_agent_renders_citations_into_the_markdown(self):
        from app.agents import graph as graph_module

        result = graph_module.ReportAgent().execute(
            {
                "research_objective": "The objective",
                "synthesized_summary": "The synthesis",
                "citations": [
                    {
                        "id": "cit-1",
                        "document_id": "d1",
                        "excerpt": "quoted text",
                        "page": 2,
                    }
                ],
                "agent_trace": [],
            }
        )
        markdown = result["final_report"]["markdown"]
        assert "The synthesis" in markdown
        assert "cit-1" in markdown and "quoted text" in markdown
