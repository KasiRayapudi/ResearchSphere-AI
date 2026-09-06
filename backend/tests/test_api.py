"""Integration tests driving the application through its HTTP API."""

import uuid

import pytest

from tests.conftest import STRONG_PASSWORD

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------- health --
class TestHealthEndpoints:
    def test_live_is_cheap_and_ok(self, client):
        response = client.get("/api/live")
        assert response.status_code == 200
        assert response.json()["status"] == "live"

    def test_health_reports_every_dependency(self, client):
        body = client.get("/api/health").json()
        assert set(body["checks"]) == {"database", "qdrant", "redis", "gemini", "storage"}
        assert body["checks"]["database"] == "ok"
        assert "uptime_seconds" in body
        assert "version" in body

    def test_ready_returns_503_when_a_dependency_is_down(self, client):
        # Qdrant and Gemini are deliberately absent in the test environment.
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"

    def test_legacy_probe_paths_still_route(self, client):
        assert client.get("/health").status_code == 200
        assert client.get("/live").status_code == 200

    def test_health_probes_are_exempt_from_rate_limiting(self, client):
        # An orchestrator polling these must not exhaust the quota.
        assert all(client.get("/api/live").status_code == 200 for _ in range(30))


class TestOpenApi:
    def test_schema_is_served(self, client):
        assert client.get("/openapi.json").status_code == 200

    def test_docs_render(self, client):
        assert client.get("/docs").status_code == 200


# ------------------------------------------------------------------ auth --
class TestAuthentication:
    def test_signup_returns_both_tokens(self, client, unique_email):
        response = client.post(
            "/api/v1/auth/signup",
            json={"name": "New", "email": unique_email, "password": STRONG_PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] and body["refresh_token"]
        assert body["user"]["email"] == unique_email

    def test_signup_rejects_weak_password_with_all_violations(self, client, unique_email):
        response = client.post(
            "/api/v1/auth/signup",
            json={"name": "Weak", "email": unique_email, "password": "password"},
        )
        assert response.status_code == 422
        assert len(response.json()["error"]["details"]) >= 3

    def test_duplicate_email_rejected(self, client, registered_user):
        response = client.post(
            "/api/v1/auth/signup",
            json={"name": "Dup", "email": registered_user["email"], "password": STRONG_PASSWORD},
        )
        assert response.status_code == 400

    def test_login_succeeds_with_correct_password(self, client, registered_user):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": STRONG_PASSWORD},
        )
        assert response.status_code == 200

    def test_login_fails_with_wrong_password(self, client, registered_user):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": registered_user["email"], "password": "WrongPass!123x"},
        )
        assert response.status_code == 401

    def test_login_fails_for_unknown_email(self, client):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": STRONG_PASSWORD},
        )
        assert response.status_code == 401

    def test_me_requires_authentication(self, client):
        # Missing credentials is 401, not 403.
        assert client.get("/api/v1/auth/me").status_code == 401

    def test_me_rejects_garbage_token(self, client):
        response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not.a.token"})
        assert response.status_code == 401

    def test_me_returns_identity(self, client, auth_headers, registered_user):
        body = client.get("/api/v1/auth/me", headers=auth_headers).json()
        assert body["email"] == registered_user["email"]

    def test_refresh_token_cannot_be_used_as_access_token(self, client, registered_user):
        headers = {"Authorization": f"Bearer {registered_user['refresh_token']}"}
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


class TestRefreshRotation:
    def test_refresh_rotates_and_invalidates_the_old_token(self, client, registered_user):
        original = registered_user["refresh_token"]

        first = client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert first.status_code == 200
        rotated = first.json()["refresh_token"]
        assert rotated != original

        # Replaying the consumed token is treated as theft.
        replay = client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert replay.status_code == 401

        # ...which revokes the whole family, including the rotated token.
        assert (
            client.post("/api/v1/auth/refresh", json={"refresh_token": rotated}).status_code == 401
        )

    def test_unknown_refresh_token_rejected(self, client):
        assert (
            client.post("/api/v1/auth/refresh", json={"refresh_token": "nonsense"}).status_code
            == 401
        )


class TestLogout:
    def test_logout_revokes_refresh_tokens(self, client, registered_user):
        response = client.post("/api/v1/auth/logout", headers=registered_user["headers"])
        assert response.status_code == 200
        assert response.json()["sessionsRevoked"] >= 1
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": registered_user["refresh_token"]}
            ).status_code
            == 401
        )


class TestPasswordReset:
    def test_request_does_not_reveal_whether_the_account_exists(self, client, registered_user):
        known = client.post(
            "/api/v1/auth/password-reset/request", json={"email": registered_user["email"]}
        )
        unknown = client.post(
            "/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"}
        )
        assert known.status_code == unknown.status_code == 200
        assert known.json()["message"] == unknown.json()["message"]
        # Only the real account gets a token (development convenience).
        assert "debug_reset_token" in known.json()
        assert "debug_reset_token" not in unknown.json()

    def test_full_reset_flow_and_single_use(self, client, unique_email):
        client.post(
            "/api/v1/auth/signup",
            json={"name": "Reset", "email": unique_email, "password": STRONG_PASSWORD},
        )
        token = client.post(
            "/api/v1/auth/password-reset/request", json={"email": unique_email}
        ).json()["debug_reset_token"]

        # Policy still applies on reset.
        assert (
            client.post(
                "/api/v1/auth/password-reset/confirm",
                json={"token": token, "new_password": "weak"},
            ).status_code
            == 422
        )

        new_password = "Zz7&nQr4$wMx9"
        assert (
            client.post(
                "/api/v1/auth/password-reset/confirm",
                json={"token": token, "new_password": new_password},
            ).status_code
            == 200
        )

        assert (
            client.post(
                "/api/v1/auth/login", json={"email": unique_email, "password": STRONG_PASSWORD}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": unique_email, "password": new_password}
            ).status_code
            == 200
        )

        # Tokens are single use.
        assert (
            client.post(
                "/api/v1/auth/password-reset/confirm",
                json={"token": token, "new_password": "Qq5#tYu8$zNb3"},
            ).status_code
            == 400
        )

    def test_strength_endpoint_scores_without_persisting(self, client):
        strong = client.post(
            "/api/v1/auth/password/strength", json={"password": "Tr0ub4dor&Kestrel"}
        ).json()
        assert strong["valid"] is True
        weak = client.post("/api/v1/auth/password/strength", json={"password": "abc"}).json()
        assert weak["valid"] is False
        assert weak["violations"]


# --------------------------------------------------------- authorization --
class TestWorkspaceIsolation:
    """Every workspace-scoped route must reject another tenant's id."""

    @pytest.fixture
    def two_users(self, client):
        def make():
            email = f"iso_{uuid.uuid4().hex[:10]}@example.com"
            body = client.post(
                "/api/v1/auth/signup",
                json={"name": "Iso", "email": email, "password": STRONG_PASSWORD},
            ).json()
            headers = {"Authorization": f"Bearer {body['access_token']}"}
            workspace = client.get("/api/v1/workspaces", headers=headers).json()[0]["id"]
            return {"headers": headers, "workspace": workspace}

        return make(), make()

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/documents",
            "/api/v1/chat/sessions",
            "/api/v1/research",
            "/api/v1/reports",
            "/api/v1/analytics",
            "/api/v1/mcp",
        ],
    )
    def test_cross_tenant_read_is_blocked(self, client, two_users, path):
        alice, bob = two_users
        response = client.get(f"{path}?workspace_id={alice['workspace']}", headers=bob["headers"])
        # 404 rather than 403 so workspace ids cannot be probed for existence.
        assert response.status_code == 404

    def test_cross_tenant_workspace_detail_is_blocked(self, client, two_users):
        alice, bob = two_users
        assert (
            client.get(
                f"/api/v1/workspaces/{alice['workspace']}", headers=bob["headers"]
            ).status_code
            == 404
        )

    def test_cross_tenant_upload_is_blocked(self, client, two_users, text_file_bytes):
        alice, bob = two_users
        response = client.post(
            "/api/v1/documents/upload",
            headers=bob["headers"],
            data={"workspace_id": alice["workspace"]},
            files={"file": ("x.txt", text_file_bytes, "text/plain")},
        )
        assert response.status_code == 404

    def test_own_workspace_still_accessible(self, client, two_users):
        _, bob = two_users
        assert (
            client.get(
                f"/api/v1/documents?workspace_id={bob['workspace']}", headers=bob["headers"]
            ).status_code
            == 200
        )


class TestAdminRbac:
    ADMIN_PATHS = [
        "/api/v1/admin/health",
        "/api/v1/admin/stats",
        "/api/v1/admin/users",
        "/api/v1/admin/feature-flags",
    ]

    @pytest.mark.parametrize("path", ADMIN_PATHS)
    def test_anonymous_gets_401(self, client, path):
        assert client.get(path).status_code == 401

    @pytest.mark.parametrize("path", ADMIN_PATHS)
    def test_non_admin_gets_403(self, client, auth_headers, path):
        assert client.get(path, headers=auth_headers).status_code == 403

    @pytest.mark.parametrize("path", ADMIN_PATHS)
    def test_admin_is_allowed(self, client, admin_user, path):
        assert client.get(path, headers=admin_user["headers"]).status_code == 200

    def test_stats_are_real_counts(self, client, admin_user):
        stats = client.get("/api/v1/admin/stats", headers=admin_user["headers"]).json()
        assert stats["users"]["total"] >= 1
        assert stats["users"]["admins"] >= 1
        assert "chunks" in stats["documents"]

    def test_user_roster_never_exposes_password_hashes(self, client, admin_user):
        body = client.get("/api/v1/admin/users", headers=admin_user["headers"]).text
        assert "$2b$" not in body
        assert "hashed_password" not in body

    def test_health_reports_celery_honestly(self, client, admin_user):
        body = client.get("/api/v1/admin/health", headers=admin_user["headers"]).json()
        # No Celery worker exists; reporting it healthy would be a fabrication.
        assert body["celeryStatus"] == "not_configured"
        assert body["uptimeSeconds"] != 1428900  # the old hardcoded value


# ---------------------------------------------------------------- routes --
class TestCoreRoutes:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/workspaces",
            "/api/v1/documents",
            "/api/v1/chat/sessions",
            "/api/v1/research",
            "/api/v1/reports",
            "/api/v1/analytics",
            "/api/v1/mcp",
        ],
    )
    def test_authenticated_routes_respond(self, client, auth_headers, path):
        assert client.get(path, headers=auth_headers).status_code == 200

    def test_workspace_can_be_created(self, client, auth_headers):
        response = client.post(
            "/api/v1/workspaces",
            headers=auth_headers,
            json={"name": "Second Workspace", "description": "created by tests"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Second Workspace"

    def test_analytics_reports_measured_values_only(self, client, auth_headers):
        body = client.get("/api/v1/analytics", headers=auth_headers).json()
        # A fresh workspace must read as empty, not as the old invented
        # 42 questions / 180 ms / 12.8 MB fallbacks.
        assert body["questionsAskedTotal"] == 0
        assert body["avgResponseTimeMs"] == 0
        assert body["storageUsageMb"] == 0
        assert all(point["queryCount"] == 0 for point in body["dailyQueries"])
        assert body["topSources"] == []


class TestUploadEndpoint:
    @pytest.mark.parametrize(
        "filename,payload",
        [
            ("evil.exe", b"MZ\x90\x00" + b"\x00" * 200),
            ("run.bat", b"@echo off\r\ndel /f /q C:\\*"),
            ("s.sh", b"#!/bin/sh\nrm -rf /"),
            ("app.jar", b"PK\x03\x04" + b"\x00" * 100),
        ],
    )
    def test_dangerous_extensions_rejected(self, client, auth_headers, filename, payload):
        response = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": (filename, payload, "application/octet-stream")},
        )
        assert response.status_code == 415

    def test_content_extension_mismatch_rejected(self, client, auth_headers):
        response = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("payload.pdf", b"MZ\x90\x00" + b"\x00" * 500, "application/pdf")},
        )
        assert response.status_code == 415

    def test_empty_file_rejected(self, client, auth_headers):
        response = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert response.status_code in (400, 415)

    def test_delete_of_unknown_document_is_404(self, client, auth_headers):
        assert (
            client.delete("/api/v1/documents/does-not-exist", headers=auth_headers).status_code
            == 404
        )


class TestErrorEnvelope:
    def test_validation_errors_use_the_standard_envelope(self, client):
        response = client.post(
            "/api/v1/auth/login", json={"email": "not-an-email", "password": "x"}
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert {"code", "message", "request_id", "timestamp", "details"} <= set(error)

    def test_request_id_is_echoed_and_sanitised(self, client):
        supplied = "req-abc-123"
        assert (
            client.get("/api/live", headers={"X-Request-ID": supplied}).headers["x-request-id"]
            == supplied
        )

        malicious = client.get("/api/live", headers={"X-Request-ID": "bad\ninjected"}).headers[
            "x-request-id"
        ]
        assert malicious != "bad\ninjected"


class TestSecurityHeaders:
    def test_headers_present_on_api_responses(self, client):
        headers = client.get("/api/live").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert headers["cross-origin-resource-policy"] == "same-site"
        assert headers["cross-origin-opener-policy"] == "same-origin"
        assert headers["content-security-policy"].startswith("default-src 'none'")

    def test_deprecated_xss_header_not_sent(self, client):
        assert "x-xss-protection" not in client.get("/api/live").headers

    def test_no_hsts_outside_production(self, client):
        assert "strict-transport-security" not in client.get("/api/live").headers

    def test_docs_receive_a_relaxed_csp(self, client):
        csp = client.get("/docs").headers["content-security-policy"]
        assert "cdn.jsdelivr.net" in csp


class TestRateLimiting:
    def test_auth_tier_returns_429_with_retry_after(self, client):
        # The autouse fixture already cleared the bucket for this test.
        codes = []
        blocked = None
        for _ in range(40):
            response = client.post(
                "/api/v1/auth/login",
                json={"email": "flood@example.com", "password": "nope"},
            )
            codes.append(response.status_code)
            if response.status_code == 429:
                blocked = response
                break
        assert 429 in codes
        assert blocked is not None
        assert "retry-after" in blocked.headers
        assert blocked.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
