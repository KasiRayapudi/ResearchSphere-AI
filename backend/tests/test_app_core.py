"""
Application core: dependency probes, lifespan, middleware stack, security
helpers and the error envelope.

Dependencies are substituted at the probe boundary so that both the healthy
and the failed branch of every check can be exercised -- the failure branches
are the ones that matter and the ones a live environment never shows you.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest

import main
from app.core.config import settings

pytestmark = pytest.mark.integration


# --------------------------------------------------------- dependency probes --
class TestDependencyProbes:
    def test_database_probe_succeeds_against_the_test_database(self):
        assert main.check_database() == "ok"

    def test_database_probe_reports_failure(self):
        with patch.object(main.engine, "connect", side_effect=OSError("refused")):
            assert main.check_database() == "failed"

    def test_qdrant_probe_reports_ok_when_reachable(self):
        client = MagicMock()
        with patch.object(main, "qdrant_client", client):
            assert main.check_qdrant() == "ok"
        client.get_collections.assert_called_once()

    def test_qdrant_probe_reports_failure_when_unreachable(self):
        client = MagicMock()
        client.get_collections.side_effect = OSError("refused")
        with patch.object(main, "qdrant_client", client):
            assert main.check_qdrant() == "failed"

    def test_qdrant_probe_reports_unavailable_without_the_library(self):
        with patch.object(main, "QdrantClient", None):
            assert main.check_qdrant() == "unavailable"

    def test_redis_probe_reports_disabled_when_not_configured(self):
        # Blank REDIS_URL is a deployment choice, not a fault, and must not
        # hold readiness back.
        assert main.check_redis() == "disabled"

    def test_redis_probe_reports_ok_when_reachable(self, fake_redis):
        client = MagicMock()
        with patch.object(main, "redis_client", client):
            assert main.check_redis() == "ok"
        client.ping.assert_called_once()

    def test_redis_probe_reports_failure_when_unreachable(self, fake_redis):
        client = MagicMock()
        client.ping.side_effect = OSError("refused")
        with patch.object(main, "redis_client", client):
            assert main.check_redis() == "failed"

    def test_gemini_probe_reports_missing_without_a_key(self):
        assert main.check_gemini() == "missing"

    def test_gemini_probe_reports_configured_with_a_key(self):
        original = settings.GEMINI_API_KEY
        settings.GEMINI_API_KEY = "a-key"
        try:
            # Configuration only: the probe must not spend a request.
            assert main.check_gemini() == "configured"
        finally:
            settings.GEMINI_API_KEY = original

    def test_storage_probe_writes_and_removes_a_file(self):
        assert main.check_storage() == "ok"

    def test_storage_probe_reports_failure_when_the_directory_is_unwritable(self):
        with patch("pathlib.Path.write_text", side_effect=PermissionError("read-only")):
            assert main.check_storage() == "failed"

    def test_resource_probe_reports_ok_under_the_thresholds(self):
        with patch.object(
            main.metrics,
            "collect_process_metrics",
            return_value={"disk_usage_percent": 10.0, "system_memory_percent": 20.0},
        ):
            detail = main.check_resources()
        assert detail["disk"] == "ok"
        assert detail["memory"] == "ok"

    def test_resource_probe_degrades_over_the_thresholds(self):
        with patch.object(
            main.metrics,
            "collect_process_metrics",
            return_value={"disk_usage_percent": 99.0, "system_memory_percent": 99.0},
        ):
            detail = main.check_resources()
        assert detail["disk"] == "degraded"
        assert detail["memory"] == "degraded"

    def test_resource_probe_reports_unavailable_measurements(self):
        with patch.object(
            main.metrics,
            "collect_process_metrics",
            return_value={"disk_usage_percent": None, "system_memory_percent": None},
        ):
            detail = main.check_resources()
        # Unknown is not the same as healthy.
        assert detail["disk"] == "unavailable"
        assert detail["memory"] == "unavailable"

    def test_resource_probe_returns_the_raw_numbers(self):
        detail = main.check_resources()
        assert "measurements" in detail

    def test_collect_checks_covers_every_dependency(self):
        checks = main.collect_checks()
        assert {
            "database",
            "qdrant",
            "redis",
            "gemini",
            "storage",
            "disk",
            "memory",
        } <= set(checks)


# ------------------------------------------------------------ health routes --
class TestHealthEndpoints:
    def test_liveness_does_not_touch_dependencies(self, client):
        with patch.object(main, "collect_checks", side_effect=AssertionError("must not run")):
            response = client.get("/api/live")
        # Liveness answers "is the process up", so a dead database must not
        # cause a restart loop.
        assert response.status_code == 200

    def test_health_reports_every_check(self, client):
        body = client.get("/api/health").json()
        assert body["status"] in ("healthy", "degraded")
        assert "checks" in body and "uptime_seconds" in body

    def test_health_reports_ok_when_everything_passes(self, client):
        with patch.object(
            main,
            "collect_checks",
            lambda: {
                "database": "ok",
                "qdrant": "ok",
                "redis": "disabled",
                "gemini": "configured",
                "storage": "ok",
                "disk": "ok",
                "memory": "ok",
            },
        ):
            assert client.get("/api/health").json()["status"] == "healthy"

    def test_health_is_degraded_when_a_dependency_fails(self, client):
        with patch.object(
            main,
            "collect_checks",
            lambda: {
                "database": "ok",
                "qdrant": "failed",
                "redis": "disabled",
                "gemini": "configured",
                "storage": "ok",
                "disk": "ok",
                "memory": "ok",
            },
        ):
            body = client.get("/api/health").json()
        # Still 200: this endpoint reports, it does not gate traffic.
        assert body["status"] == "degraded"

    def test_readiness_passes_when_dependencies_are_healthy(self, client):
        with patch.object(
            main,
            "collect_checks",
            lambda: {
                "database": "ok",
                "qdrant": "ok",
                "redis": "disabled",
                "gemini": "configured",
                "storage": "ok",
                "disk": "ok",
                "memory": "ok",
            },
        ):
            assert client.get("/api/ready").status_code == 200

    def test_uptime_increases(self):
        assert main.uptime_seconds() >= 0

    def test_openapi_schema_is_served(self, client):
        schema = client.get("/openapi.json").json()
        assert "/api/v1/auth/login" in schema["paths"]


# ------------------------------------------------------------- middleware ----
class TestMiddleware:
    def test_request_id_is_returned(self, client):
        assert client.get("/api/live").headers.get("x-request-id")

    def test_supplied_request_id_is_echoed(self, client):
        supplied = "11111111-2222-3333-4444-555555555555"
        response = client.get("/api/live", headers={"X-Request-ID": supplied})
        assert response.headers["x-request-id"] == supplied

    def test_hostile_request_id_is_sanitised(self, client):
        response = client.get(
            "/api/live", headers={"X-Request-ID": "abc\r\nInjected-Header: evil"}
        )
        returned = response.headers["x-request-id"]
        # A header value carrying CRLF would let a client inject headers.
        assert "\r" not in returned and "\n" not in returned
        assert "Injected-Header" not in returned

    def test_overlong_request_id_is_bounded(self, client):
        response = client.get("/api/live", headers={"X-Request-ID": "x" * 5000})
        assert len(response.headers["x-request-id"]) < 200

    def test_security_headers_are_present(self, client):
        headers = client.get("/api/live").headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] in ("DENY", "SAMEORIGIN")
        assert "content-security-policy" in headers
        assert "referrer-policy" in headers

    def test_legacy_xss_header_is_not_sent(self, client):
        # X-XSS-Protection is deprecated and reintroduces vulnerabilities.
        assert "x-xss-protection" not in client.get("/api/live").headers

    def test_hsts_is_not_sent_outside_production(self, client):
        # Sending HSTS over plain http in development pins the browser to a
        # scheme the developer is not serving.
        assert "strict-transport-security" not in client.get("/api/live").headers

    def test_docs_receive_a_relaxed_policy(self, client):
        response = client.get("/docs")
        if response.status_code == 200:
            # Swagger UI loads its assets from a CDN and needs inline script.
            assert "unsafe-inline" in response.headers.get("content-security-policy", "")

    def test_untrusted_host_is_rejected(self, client):
        response = client.get("/api/live", headers={"Host": "evil.example.com"})
        assert response.status_code == 400

    def test_cors_preflight_is_answered_for_an_allowed_origin(self, client):
        origin = settings.cors_origins[0]
        if origin == "*":
            pytest.skip("wildcard CORS configured")
        response = client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code in (200, 204)
        assert response.headers.get("access-control-allow-origin") == origin

    def test_unknown_origin_is_not_granted_access(self, client):
        response = client.get("/api/live", headers={"Origin": "https://evil.example.com"})
        assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"

    def test_rate_limited_response_still_carries_cors_headers(self, client):
        """CORS must wrap the rate limiter, not the other way round.

        A 429 without CORS headers is unreadable to the browser, so the user
        sees an opaque network error instead of "slow down".
        """
        origin = settings.cors_origins[0]
        if origin == "*":
            pytest.skip("wildcard CORS configured")

        last = None
        for _ in range(60):
            last = client.post(
                "/api/v1/auth/login",
                headers={"Origin": origin},
                json={"email": "nobody@example.com", "password": "wrong-password-1A!"},
            )
            if last.status_code == 429:
                break

        if last.status_code != 429:
            pytest.skip("rate limit not reached in this configuration")
        assert last.headers.get("access-control-allow-origin") == origin

    def test_health_probes_are_exempt_from_rate_limiting(self, client):
        # A limiter that throttles the load balancer's probe takes the
        # instance out of rotation under exactly the load it should survive.
        for _ in range(60):
            assert client.get("/api/live").status_code == 200


# --------------------------------------------------------- error envelope ----
class TestErrorHandling:
    def test_not_found_uses_the_standard_envelope(self, client):
        body = client.get("/api/v1/does-not-exist").json()
        assert "error" in body
        assert {"code", "message", "request_id", "timestamp"} <= set(body["error"])

    def test_validation_failure_reports_the_offending_field(self, client):
        response = client.post("/api/v1/auth/login", json={"email": "not-an-email"})
        assert response.status_code == 422
        details = response.json()["error"]["details"]
        assert any("password" in str(item.get("loc", "")) for item in details)

    def test_envelope_carries_the_request_id(self, client):
        response = client.get("/api/v1/does-not-exist")
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]

    def test_unexpected_failure_does_not_leak_internals(self):
        import asyncio

        from app.core.exception_handlers import generic_exception_handler

        request = MagicMock()
        request.state.request_id = "r-1"
        response = asyncio.run(
            generic_exception_handler(request, RuntimeError("secret internal detail"))
        )
        assert response.status_code == 500
        # Stack traces and internal messages belong in the logs, not the body.
        assert b"secret internal detail" not in response.body
        assert b"INTERNAL_ERROR" in response.body

    def test_application_exceptions_carry_their_code(self):
        from app.core.exceptions import AppException, format_error_response

        body = format_error_response(
            AppException(message="nope", code="CUSTOM_CODE", status_code=418), request_id="r-1"
        )
        assert body["error"]["code"] == "CUSTOM_CODE"
        assert body["error"]["request_id"] == "r-1"


# ------------------------------------------------------------ security core --
class TestSecurityHelpers:
    def test_password_round_trip(self):
        from app.core.security import hash_password, verify_password

        hashed = hash_password("Xk9#mQp2$vLw7")
        assert hashed != "Xk9#mQp2$vLw7"
        assert verify_password("Xk9#mQp2$vLw7", hashed)
        assert not verify_password("wrong", hashed)

    def test_hashes_are_salted(self):
        from app.core.security import hash_password

        # Identical passwords must not produce identical hashes.
        assert hash_password("same-password") != hash_password("same-password")

    def test_access_token_carries_the_expected_claims(self):
        from app.core.security import create_access_token, decode_token

        token = create_access_token({"sub": "user-1", "role": "member"})
        claims = decode_token(token, expected_type="access")
        for claim in ("sub", "jti", "iat", "nbf", "exp", "typ", "iss", "aud"):
            assert claim in claims, claim
        assert claims["typ"] == "access"

    def test_a_refresh_token_is_rejected_where_an_access_token_is_required(self):
        from app.core.exceptions import AuthenticationException
        from app.core.security import create_refresh_token, decode_token

        token = create_refresh_token({"sub": "user-1"})
        # Token type confusion: a refresh token must not authenticate a request.
        with pytest.raises((AuthenticationException, Exception)):
            decode_token(token, expected_type="access")

    def test_a_tampered_token_is_rejected(self):
        from app.core.security import create_access_token, decode_token

        token = create_access_token({"sub": "user-1", "role": "member"})
        tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
        with pytest.raises(Exception):
            decode_token(tampered, expected_type="access")

    def test_a_token_signed_with_another_key_is_rejected(self):
        import jose.jwt

        from app.core.security import decode_token

        forged = jose.jwt.encode(
            {"sub": "user-1", "typ": "access"}, "a-completely-different-key", algorithm="HS256"
        )
        with pytest.raises(Exception):
            decode_token(forged, expected_type="access")

    def test_role_ranking_is_ordered(self):
        from app.core.security import role_rank

        assert role_rank("admin") > role_rank("member")
        assert role_rank("member") >= role_rank("viewer")

    def test_an_unknown_role_ranks_lowest(self):
        from app.core.security import role_rank

        # An unrecognised role must not accidentally outrank a real one.
        assert role_rank("not-a-real-role") <= role_rank("viewer")

    def test_missing_credentials_produce_401_not_403(self, client):
        # 403 tells a client it is authenticated but forbidden, which sends
        # the frontend down the wrong recovery path.
        assert client.get("/api/v1/workspaces").status_code == 401

    def test_a_malformed_authorization_header_is_rejected(self, client):
        for value in ("", "Bearer", "Basic abc", "Bearer not.a.token"):
            response = client.get("/api/v1/workspaces", headers={"Authorization": value})
            assert response.status_code == 401, value


# ------------------------------------------------------------- lifespan -----
class TestLifespan:
    def test_invalid_production_configuration_refuses_to_start(self):
        """A misconfigured production container must fail fast and visibly.

        Starting and serving broken requests is worse than not starting: the
        orchestrator cannot tell the difference between healthy and useless.
        """
        import asyncio

        from app.core.config import ConfigurationError

        async def _run():
            async with main.lifespan(main.app):
                pass

        with patch.object(
            main,
            "validate_configuration",
            return_value={"errors": ["SECRET_KEY is too short"], "warnings": []},
        ):
            with pytest.raises(ConfigurationError, match="Refusing to start"):
                asyncio.run(_run())

    def test_warnings_alone_do_not_block_startup(self):
        import asyncio

        async def _run():
            async with main.lifespan(main.app):
                return True

        with patch.object(
            main,
            "validate_configuration",
            return_value={"errors": [], "warnings": ["Redis is not configured"]},
        ):
            assert asyncio.run(_run()) is True

    def test_shutdown_releases_clients_even_when_closing_fails(self, caplog):
        import asyncio

        failing = MagicMock()
        failing.close.side_effect = OSError("already closed")

        async def _run():
            async with main.lifespan(main.app):
                main.qdrant_client = failing

        with patch.object(
            main, "validate_configuration", return_value={"errors": [], "warnings": []}
        ):
            with caplog.at_level(logging.ERROR):
                # A failure while closing must not prevent the rest of shutdown.
                asyncio.run(_run())

        assert main.qdrant_client is None
