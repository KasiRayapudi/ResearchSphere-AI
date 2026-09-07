"""Tests for metrics, health checks, tracking hooks and slow-request logging."""

import logging

import pytest

from app.core import metrics
from app.core.config import settings

pytestmark = pytest.mark.integration


def _parse_exposition(text: str) -> dict[str, float]:
    """Parse the Prometheus text format into {series: value}."""
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        try:
            samples[name.strip()] = float(value)
        except ValueError:
            continue
    return samples


class TestMetricsEndpoint:
    def test_metrics_are_exposed(self, client):
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")

    def test_exposition_declares_expected_families(self, client):
        body = client.get("/metrics").text
        for family in (
            "researchsphere_http_requests_total",
            "researchsphere_http_request_duration_seconds",
            "researchsphere_auth_attempts_total",
            "researchsphere_uploads_total",
            "researchsphere_chat_messages_total",
            "researchsphere_dependency_up",
            "researchsphere_app_info",
        ):
            assert family in body, family

    def test_metrics_route_is_not_in_the_public_schema(self, client):
        schema = client.get("/openapi.json").json()
        assert "/metrics" not in schema["paths"]

    def test_endpoint_can_be_disabled(self, client):
        original = settings.METRICS_ENABLED
        settings.METRICS_ENABLED = False
        try:
            assert client.get("/metrics").status_code == 404
        finally:
            settings.METRICS_ENABLED = original

    def test_token_is_required_when_configured(self, client):
        original = settings.METRICS_TOKEN
        settings.METRICS_TOKEN = "scrape-secret"
        try:
            assert client.get("/metrics").status_code == 401
            assert (
                client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
            )
            assert (
                client.get(
                    "/metrics", headers={"Authorization": "Bearer scrape-secret"}
                ).status_code
                == 200
            )
        finally:
            settings.METRICS_TOKEN = original


class TestRequestMetrics:
    def test_requests_are_counted_by_route_template(self, client, auth_headers):
        client.get("/api/v1/workspaces", headers=auth_headers)
        samples = _parse_exposition(client.get("/metrics").text)

        counted = [
            key
            for key in samples
            if key.startswith("researchsphere_http_requests_total")
            and 'route="/api/v1/workspaces"' in key
        ]
        assert counted, "no counter series for the workspaces route"

    def test_labels_use_templates_not_concrete_ids(self, client, auth_headers):
        # The whole point of template labels: a request for a specific document
        # must not mint a series containing that id.
        client.delete("/api/v1/documents/some-specific-id-1234", headers=auth_headers)
        body = client.get("/metrics").text
        assert "some-specific-id-1234" not in body
        assert 'route="/api/v1/documents/{id}"' in body

    def test_duration_histogram_is_recorded(self, client, auth_headers):
        client.get("/api/v1/documents", headers=auth_headers)
        body = client.get("/metrics").text
        assert "researchsphere_http_request_duration_seconds_bucket" in body

    def test_health_endpoints_are_excluded(self, client):
        client.get("/api/live")
        body = client.get("/metrics").text
        assert 'route="/api/live"' not in body

    def test_response_time_header_is_present(self, client, auth_headers):
        response = client.get("/api/v1/workspaces", headers=auth_headers)
        assert "x-response-time-ms" in response.headers
        assert float(response.headers["x-response-time-ms"]) >= 0


class TestDomainMetrics:
    def test_failed_login_is_counted(self, client, registered_user):
        before = _parse_exposition(client.get("/metrics").text)
        key = 'researchsphere_auth_failures_total{action="login",reason="bad_password"}'
        start = before.get(key, 0.0)

        client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": "WrongPass!123x"},
        )

        after = _parse_exposition(client.get("/metrics").text)
        assert after.get(key, 0.0) == start + 1

    def test_rejected_upload_is_counted_with_a_reason(self, client, auth_headers):
        client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("bad.exe", b"MZ\x90\x00" + b"\x00" * 200, "application/octet-stream")},
        )
        samples = _parse_exposition(client.get("/metrics").text)
        assert samples.get('researchsphere_uploads_total{outcome="rejected"}', 0) >= 1
        assert any(
            key.startswith("researchsphere_upload_rejections_total") and "blocked_extension" in key
            for key in samples
        )

    def test_authorization_denial_is_counted(self, client, auth_headers):
        client.get("/api/v1/admin/stats", headers=auth_headers)  # non-admin -> 403
        samples = _parse_exposition(client.get("/metrics").text)
        assert samples.get('researchsphere_authz_denials_total{reason="not_admin"}', 0) >= 1


class TestDependencyGauges:
    def test_dependency_state_is_exported(self, client):
        client.get("/api/health")
        samples = _parse_exposition(client.get("/metrics").text)
        assert samples.get('researchsphere_dependency_up{dependency="database"}') == 1.0
        # Qdrant is genuinely unreachable in the test environment.
        assert samples.get('researchsphere_dependency_up{dependency="qdrant"}') == 0.0
        # Redis is not configured, which is distinct from being down.
        assert samples.get('researchsphere_dependency_up{dependency="redis"}') == -1.0

    def test_probe_latency_is_recorded(self, client):
        client.get("/api/health")
        assert (
            "researchsphere_dependency_check_duration_seconds_bucket" in client.get("/metrics").text
        )


class TestHealthResourceChecks:
    def test_health_includes_disk_and_memory(self, client):
        checks = client.get("/api/health").json()["checks"]
        assert "disk" in checks
        assert "memory" in checks

    def test_process_metrics_are_sampled(self, client):
        client.get("/metrics")
        samples = _parse_exposition(client.get("/metrics").text)
        assert samples.get("researchsphere_process_memory_bytes", 0) > 0
        assert "researchsphere_system_memory_percent" in samples

    def test_resource_sample_reports_real_numbers(self):
        sample = metrics.collect_process_metrics()
        assert sample["process_memory_bytes"] > 0
        assert 0 <= sample["system_memory_percent"] <= 100
        assert 0 <= sample["disk_usage_percent"] <= 100


class TestSlowRequestLogging:
    def test_slow_request_is_logged_and_counted(self, client, auth_headers, caplog):
        original = settings.SLOW_REQUEST_THRESHOLD_MS
        # Any request is "slow" at a 0 ms threshold.
        settings.SLOW_REQUEST_THRESHOLD_MS = 0
        try:
            with caplog.at_level(logging.WARNING, logger="researchsphere.middleware"):
                client.get("/api/v1/workspaces", headers=auth_headers)
            assert any("Slow request" in record.message for record in caplog.records)
        finally:
            settings.SLOW_REQUEST_THRESHOLD_MS = original

        assert "researchsphere_http_slow_requests_total" in client.get("/metrics").text


class TestErrorTracking:
    def test_default_backend_is_logging(self):
        from app.core import tracking

        tracking.reset_tracker()
        assert tracking.get_tracker().name == "logging"

    def test_capture_records_the_exception_with_context(self, caplog):
        from app.core import tracking

        tracking.reset_tracker()
        with caplog.at_level(logging.ERROR, logger="researchsphere.tracking"):
            tracking.capture_exception(ValueError("boom"), route="test", workspace_id="ws-1")
        assert any("ValueError" in record.message for record in caplog.records)

    def test_context_is_redacted(self, caplog):
        from app.core import tracking

        tracking.reset_tracker()
        with caplog.at_level(logging.ERROR, logger="researchsphere.tracking"):
            tracking.capture_exception(ValueError("boom"), password="hunter2")
        for record in caplog.records:
            assert "hunter2" not in str(getattr(record, "tracking_context", ""))

    def test_capture_never_raises(self):
        from app.core import tracking

        # Reporting an error must not become a second error.
        tracking.capture_exception(RuntimeError("x"), unserialisable=object())


class TestMetricsHelpers:
    def test_safe_swallows_failures(self):
        # Instrumentation must never break a request.
        metrics.safe(lambda: 1 / 0)

    def test_track_duration_records_even_when_the_block_raises(self):
        with pytest.raises(RuntimeError):
            with metrics.track_duration(metrics.embedding_duration_seconds):
                raise RuntimeError("failure inside the timed block")

    @pytest.mark.parametrize(
        "status,expected",
        [("ok", 1.0), ("configured", 1.0), ("failed", 0.0), ("disabled", -1.0), ("missing", -1.0)],
    )
    def test_dependency_status_mapping(self, status, expected):
        metrics.observe_dependency("probe_test", status, 0.01)
        rendered = metrics.render().decode()
        assert f'researchsphere_dependency_up{{dependency="probe_test"}} {expected}' in rendered


class TestReadinessVersusHealth:
    """Resource pressure must warn without removing the instance from rotation."""

    def test_degraded_resources_do_not_fail_readiness(self, client, monkeypatch):
        import main

        monkeypatch.setattr(
            main,
            "collect_checks",
            lambda: {
                "database": "ok",
                "qdrant": "ok",
                "redis": "disabled",
                "gemini": "configured",
                "storage": "ok",
                "disk": "degraded",
                "memory": "degraded",
            },
        )
        # Ready: pulling a healthy instance out of the load balancer because a
        # disk is 85% full would concentrate traffic and worsen the incident.
        assert client.get("/api/ready").status_code == 200
        # ...but /api/health still reports it so it is visible and alertable.
        assert client.get("/api/health").json()["status"] == "degraded"

    def test_failed_dependency_still_fails_readiness(self, client, monkeypatch):
        import main

        monkeypatch.setattr(
            main,
            "collect_checks",
            lambda: {
                "database": "failed",
                "qdrant": "ok",
                "redis": "disabled",
                "gemini": "configured",
                "storage": "ok",
                "disk": "ok",
                "memory": "ok",
            },
        )
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert "database" in response.json()["failed"]
