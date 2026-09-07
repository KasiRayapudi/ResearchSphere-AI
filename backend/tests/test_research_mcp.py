"""
Research sessions and MCP connectors.

Both routes had the same two problems as the report route: content presented
to the user that no run had produced, and lookups by id with no ownership
check. These tests pin down the corrected behaviour.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


GRAPH_OUTPUT = {
    "synthesized_summary": "A synthesis of the retrieved material.",
    "final_report": {"markdown": "# Findings"},
    "citations": [{"document_id": "doc-1", "snippet": "cited"}],
    "confidence_score": 0.72,
    "critic_verified": True,
    "agent_trace": [
        {"agent": "Planner", "task": "Generated plan", "status": "completed", "time_ms": 61},
        {"agent": "Retriever", "task": "Retrieved 3 chunks", "status": "completed", "time_ms": 240},
    ],
}


def _other_account(client, email):
    from tests.conftest import STRONG_PASSWORD

    response = client.post(
        "/api/v1/auth/signup",
        json={"name": "Other", "email": email, "password": STRONG_PASSWORD},
    )
    if response.status_code != 200:
        response = client.post(
            "/api/v1/auth/login", json={"email": email, "password": STRONG_PASSWORD}
        )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --------------------------------------------------------------- research ---
class TestResearchSessions:
    def test_listing_requires_authentication(self, client):
        assert client.get("/api/v1/research").status_code == 401

    def test_starting_requires_authentication(self, client):
        response = client.post("/api/v1/research/start", json={"title": "t", "objective": "o"})
        assert response.status_code == 401

    def test_new_account_has_no_sessions(self, client, auth_headers):
        assert client.get("/api/v1/research", headers=auth_headers).json() == []

    def _start(self, client, headers, workspace_id, output=None):
        from app.api.v1 import research

        with patch.object(research.engine, "run_graph", return_value=output or GRAPH_OUTPUT):
            return client.post(
                "/api/v1/research/start",
                headers=headers,
                json={
                    "title": "Market Analysis",
                    "objective": "Assess the market",
                    "workspace_id": workspace_id,
                },
            )

    def test_session_is_created(self, client, auth_headers, workspace_id):
        response = self._start(client, auth_headers, workspace_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["title"] == "Market Analysis"
        assert body["status"] == "ready"
        assert body["progressPercentage"] == 100

    def test_returned_steps_are_the_steps_that_ran(self, client, auth_headers, workspace_id):
        body = self._start(client, auth_headers, workspace_id).json()
        steps = body["agentSteps"]
        assert [s["agentName"] for s in steps] == ["Planner", "Retriever"]
        assert [s["executionTimeMs"] for s in steps] == [61, 240]

    def test_listing_returns_the_recorded_trace_not_a_fixed_script(
        self, client, auth_headers, workspace_id
    ):
        """Regression: every listed session used to return the same four steps.

        The listing hardcoded Planner/Retriever/Researcher/Critic with invented
        task text and timings (75/120/450/110 ms) regardless of what the run
        actually did, and timestamps of "1 min ago"/"Just now".
        """
        self._start(client, auth_headers, workspace_id)
        listed = client.get("/api/v1/research", headers=auth_headers).json()
        assert len(listed) == 1

        steps = listed[0]["agentSteps"]
        assert [s["agentName"] for s in steps] == ["Planner", "Retriever"]
        assert [s["task"] for s in steps] == ["Generated plan", "Retrieved 3 chunks"]
        assert [s["executionTimeMs"] for s in steps] == [61, 240]
        # Timestamps must be real, not relative prose.
        assert all(s["timestamp"] not in ("Just now", "1 min ago") for s in steps)

    def test_a_run_with_no_steps_reports_none(self, client, auth_headers, workspace_id):
        output = dict(GRAPH_OUTPUT, agent_trace=[])
        self._start(client, auth_headers, workspace_id, output=output)
        listed = client.get("/api/v1/research", headers=auth_headers).json()
        # An empty list is honest; inventing steps is not.
        assert listed[0]["agentSteps"] == []

    def test_confidence_is_persisted_and_reported_truthfully(
        self, client, auth_headers, workspace_id
    ):
        self._start(client, auth_headers, workspace_id)

        from app.core.database import SessionLocal
        from app.models.report import Report

        session = SessionLocal()
        try:
            report = session.query(Report).order_by(Report.created_at.desc()).first()
            assert report.confidence_score == 0.72
            assert report.agent_trace
            assert "0.72" in report.technical_analysis
        finally:
            session.close()

    def test_unverified_run_is_not_described_as_verified(self, client, auth_headers, workspace_id):
        output = dict(GRAPH_OUTPUT, critic_verified=False, confidence_score=0.0)
        self._start(client, auth_headers, workspace_id, output=output)

        from app.core.database import SessionLocal
        from app.models.report import Report

        session = SessionLocal()
        try:
            report = session.query(Report).order_by(Report.created_at.desc()).first()
            assert "could not verify" in report.technical_analysis
        finally:
            session.close()

    def test_source_count_reflects_the_citations(self, client, auth_headers, workspace_id):
        assert self._start(client, auth_headers, workspace_id).json()["sourcesCount"] == 1

    def test_graph_failure_returns_500(self, client, auth_headers, workspace_id):
        from app.api.v1 import research

        with patch.object(research.engine, "run_graph", side_effect=RuntimeError("graph broke")):
            response = client.post(
                "/api/v1/research/start",
                headers=auth_headers,
                json={"title": "t", "objective": "o", "workspace_id": workspace_id},
            )
        assert response.status_code == 500
        assert (
            'researchsphere_research_sessions_total{outcome="failed"}'
            in client.get("/metrics").text
        )

    def test_graph_runs_off_the_event_loop(self, client, auth_headers, workspace_id):
        """The graph must not be invoked inline from the async handler.

        Doing so blocks every other request for the length of the run, and the
        retriever cannot start its own loop from a thread that already has one.
        """
        import asyncio

        seen = {}

        def _run_graph(objective, workspace_id, document_ids=None):
            try:
                asyncio.get_running_loop()
                seen["on_loop"] = True
            except RuntimeError:
                seen["on_loop"] = False
            return GRAPH_OUTPUT

        from app.api.v1 import research

        with patch.object(research.engine, "run_graph", _run_graph):
            client.post(
                "/api/v1/research/start",
                headers=auth_headers,
                json={"title": "t", "objective": "o", "workspace_id": workspace_id},
            )
        assert seen["on_loop"] is False

    def test_sessions_are_not_visible_across_accounts(self, client, auth_headers, workspace_id):
        self._start(client, auth_headers, workspace_id)
        headers = _other_account(client, "research_isolation@example.com")
        assert client.get("/api/v1/research", headers=headers).json() == []

    def test_starting_in_another_users_workspace_is_refused(
        self, client, auth_headers, workspace_id
    ):
        from app.api.v1 import research

        headers = _other_account(client, "research_outsider@example.com")
        with patch.object(research.engine, "run_graph", return_value=GRAPH_OUTPUT) as graph:
            response = client.post(
                "/api/v1/research/start",
                headers=headers,
                json={"title": "t", "objective": "o", "workspace_id": workspace_id},
            )
        assert response.status_code in (400, 403, 404)
        graph.assert_not_called()


# ------------------------------------------------------------- connectors ---
class TestMCPConnectors:
    def test_listing_requires_authentication(self, client):
        assert client.get("/api/v1/mcp").status_code == 401

    def test_registry_connectors_are_materialised(self, client, auth_headers, workspace_id):
        connectors = client.get(
            f"/api/v1/mcp?workspace_id={workspace_id}", headers=auth_headers
        ).json()
        assert connectors
        assert all({"id", "name", "provider", "status"} <= set(c) for c in connectors)

    def test_listing_is_idempotent(self, client, auth_headers, workspace_id):
        first = client.get(f"/api/v1/mcp?workspace_id={workspace_id}", headers=auth_headers).json()
        second = client.get(f"/api/v1/mcp?workspace_id={workspace_id}", headers=auth_headers).json()
        # Reconciliation must not insert a duplicate row on every request.
        assert len(first) == len(second)

    def test_future_connectors_are_flagged(self, client, auth_headers, workspace_id):
        connectors = client.get(
            f"/api/v1/mcp?workspace_id={workspace_id}", headers=auth_headers
        ).json()
        assert any(c["is_future_connector"] for c in connectors)

    def _live_connector(self, client, headers, workspace_id):
        connectors = client.get(f"/api/v1/mcp?workspace_id={workspace_id}", headers=headers).json()
        return next(c for c in connectors if not c["is_future_connector"])

    def test_owner_can_toggle(self, client, auth_headers, workspace_id):
        connector = self._live_connector(client, auth_headers, workspace_id)
        before = connector["status"]

        response = client.post(f"/api/v1/mcp/{connector['id']}/toggle", headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["status"] != before

    def test_toggle_is_reversible(self, client, auth_headers, workspace_id):
        connector = self._live_connector(client, auth_headers, workspace_id)
        first = client.post(f"/api/v1/mcp/{connector['id']}/toggle", headers=auth_headers).json()[
            "status"
        ]
        second = client.post(f"/api/v1/mcp/{connector['id']}/toggle", headers=auth_headers).json()[
            "status"
        ]
        assert first != second

    def test_another_users_connector_cannot_be_toggled(self, client, auth_headers, workspace_id):
        """Regression: the toggle looked the connector up by id alone.

        Any authenticated user could enable or disable any tenant's connectors
        by guessing or observing an id.
        """
        connector = self._live_connector(client, auth_headers, workspace_id)
        before = connector["status"]

        headers = _other_account(client, "mcp_attacker@example.com")
        response = client.post(f"/api/v1/mcp/{connector['id']}/toggle", headers=headers)
        # 404 rather than 403, so ids cannot be probed.
        assert response.status_code == 404

        after = self._live_connector(client, auth_headers, workspace_id)["status"]
        assert after == before

    def test_unknown_connector_returns_404(self, client, auth_headers):
        response = client.post("/api/v1/mcp/no-such-connector/toggle", headers=auth_headers)
        assert response.status_code == 404

    def test_future_connector_toggle_reports_disconnected(self, client, auth_headers, workspace_id):
        connectors = client.get(
            f"/api/v1/mcp?workspace_id={workspace_id}", headers=auth_headers
        ).json()
        future = next(c for c in connectors if c["is_future_connector"])

        response = client.post(f"/api/v1/mcp/{future['id']}/toggle", headers=auth_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["is_future_connector"] is True
        # Nothing is connected, and the response says so rather than pretending.
        assert body["status"] == "disconnected"

    def test_toggle_requires_authentication(self, client):
        assert client.post("/api/v1/mcp/some-id/toggle").status_code == 401

    def test_connectors_are_not_shared_across_accounts(self, client, auth_headers, workspace_id):
        mine = client.get(f"/api/v1/mcp?workspace_id={workspace_id}", headers=auth_headers).json()
        my_ids = {c["id"] for c in mine if not c["is_future_connector"]}

        headers = _other_account(client, "mcp_isolation@example.com")
        theirs = client.get("/api/v1/mcp", headers=headers).json()
        their_ids = {c["id"] for c in theirs if not c["is_future_connector"]}

        assert my_ids and their_ids
        assert my_ids.isdisjoint(their_ids)


class TestConnectorRegistry:
    def test_registry_lists_connectors(self):
        from app.mcp.registry import MCPConnectorRegistry

        connectors = MCPConnectorRegistry().list_all_connectors()
        assert connectors
        for item in connectors:
            assert {"connector_id", "name", "provider", "status"} <= set(item)

    def test_every_entry_declares_whether_it_is_future(self):
        from app.mcp.registry import MCPConnectorRegistry

        connectors = MCPConnectorRegistry().list_all_connectors()
        # The route branches on this key, so it must always be present.
        assert all("is_future_connector" in item for item in connectors)

    def test_no_connector_claims_documents_it_never_synced(self):
        """Regression: the three built-in connectors reported fixed counts.

        GitHub claimed 342 documents, Google Drive 128 and local files 56,
        with status "connected", for integrations that have no credentials,
        no transport and no sync job. Those counts were written into the
        connectors table and shown to users as real activity.
        """
        from app.mcp.registry import MCPConnectorRegistry

        for item in MCPConnectorRegistry().list_all_connectors():
            assert item["synced_count"] == 0, item["name"]
            assert item["status"] == "disconnected", item["name"]

    def test_syncing_an_unimplemented_connector_raises(self):
        from app.mcp.github import GitHubConnector, GoogleDriveConnector, LocalFilesConnector

        for connector in (GitHubConnector(), GoogleDriveConnector(), LocalFilesConnector()):
            with pytest.raises(NotImplementedError, match="not implemented"):
                # Returning invented documents here would put fabricated
                # content into the index.
                connector.sync_data()

    def test_connector_ids_are_unique(self):
        from app.mcp.registry import MCPConnectorRegistry

        ids = [c["connector_id"] for c in MCPConnectorRegistry().list_all_connectors()]
        assert len(ids) == len(set(ids))
