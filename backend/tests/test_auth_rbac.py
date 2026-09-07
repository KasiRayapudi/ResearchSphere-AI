"""
Authentication dependencies, role-based access control, configuration edges,
logging, error tracking and the remaining RAG boundaries.

These are the branches a working environment never exercises: a token for a
deleted account, a deactivated user, a revoked jti, a role that is almost but
not quite sufficient.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.integration


# ------------------------------------------------------- current-user path --
class TestCurrentUser:
    def test_a_valid_token_resolves_its_user(self, client, registered_user):
        response = client.get("/api/v1/auth/me", headers=registered_user["headers"])
        assert response.status_code == 200
        assert response.json()["email"] == registered_user["email"]

    def test_a_token_without_a_subject_is_rejected(self, client):
        from app.core.security import create_access_token

        token = create_access_token({"role": "member"})
        response = client.get("/api/v1/workspaces", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_a_token_for_a_deleted_account_is_rejected(self, client):
        from app.core.security import create_access_token

        token = create_access_token({"sub": "a-user-that-never-existed", "role": "member"})
        response = client.get("/api/v1/workspaces", headers={"Authorization": f"Bearer {token}"})
        # A valid signature is not enough: the account must still exist.
        assert response.status_code == 401

    def test_a_deactivated_account_is_rejected(self, client, registered_user):
        from app.core.database import SessionLocal
        from app.models.user import User

        session = SessionLocal()
        try:
            user = session.query(User).filter(User.email == registered_user["email"]).first()
            user.is_active = False
            session.commit()
        finally:
            session.close()

        # Deactivation must take effect immediately, not at token expiry.
        response = client.get("/api/v1/workspaces", headers=registered_user["headers"])
        assert response.status_code == 401

    def test_a_revoked_token_is_rejected(self, fake_redis, registered_user, client):
        from app.core.security import decode_token
        from app.core.token_store import revoke_token

        claims = decode_token(registered_user["access_token"], check_revocation=False)
        revoke_token(claims["jti"], exp=claims["exp"])

        with pytest.raises(HTTPException) as raised:
            decode_token(registered_user["access_token"])
        assert raised.value.status_code == 401

    def test_revocation_can_be_skipped_for_rotation(self, fake_redis, registered_user):
        """Rotation must consult the database, not the blacklist.

        Rotation blacklists the token it just consumed. If the refresh path
        then re-checked the blacklist it would reject a replay before the
        family could be revoked, defeating theft detection exactly when Redis
        is available.
        """
        from app.core.security import decode_token
        from app.core.token_store import revoke_token

        claims = decode_token(registered_user["access_token"], check_revocation=False)
        revoke_token(claims["jti"], exp=claims["exp"])

        still_decodable = decode_token(registered_user["access_token"], check_revocation=False)
        assert still_decodable["sub"] == claims["sub"]


# --------------------------------------------------------------------- RBAC --
class TestRoleEnforcement:
    def test_admin_endpoint_rejects_a_member(self, client, auth_headers):
        response = client.get("/api/v1/admin/stats", headers=auth_headers)
        # 403, not 404 or 401: the caller is known, just not permitted.
        assert response.status_code == 403

    def test_admin_endpoint_accepts_an_admin(self, client, admin_user):
        assert client.get("/api/v1/admin/stats", headers=admin_user["headers"]).status_code == 200

    def test_admin_endpoint_rejects_an_anonymous_caller(self, client):
        assert client.get("/api/v1/admin/stats").status_code == 401

    def test_a_denial_is_counted(self, client, auth_headers):
        client.get("/api/v1/admin/stats", headers=auth_headers)
        assert "researchsphere_authz_denials_total" in client.get("/metrics").text

    def test_require_role_accepts_a_higher_rank(self):
        from app.core.security import require_role

        dependency = require_role("member")
        admin = MagicMock(role="admin")
        # An admin satisfies a member requirement; ranks are ordered.
        assert dependency(request=MagicMock(), current_user=admin) is admin

    def test_require_role_rejects_a_lower_rank(self):
        from app.core.security import require_role

        dependency = require_role("admin")
        with pytest.raises(HTTPException) as raised:
            dependency(request=MagicMock(), current_user=MagicMock(role="viewer"))
        assert raised.value.status_code == 403

    def test_require_role_rejects_a_missing_role(self):
        from app.core.security import require_role

        dependency = require_role("admin")
        user = MagicMock()
        user.role = None
        with pytest.raises(HTTPException):
            dependency(request=MagicMock(), current_user=user)

    def test_role_matching_is_case_insensitive(self):
        from app.core.security import require_role

        dependency = require_role("Admin")
        user = MagicMock(role="ADMIN")
        assert dependency(request=MagicMock(), current_user=user) is user

    def test_a_token_claiming_admin_does_not_grant_admin(self, client, registered_user):
        """Privilege must be read from the database, not from the token.

        A forged or stale token asserting role=admin must not elevate an
        account that is not an admin.
        """
        from app.core.security import create_access_token

        token = create_access_token({"sub": registered_user["user"]["id"], "role": "admin"})
        response = client.get("/api/v1/admin/stats", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403


class TestWorkspaceResolution:
    def test_the_default_workspace_is_used_when_none_is_given(self, client, auth_headers):
        assert client.get("/api/v1/documents", headers=auth_headers).status_code == 200

    def test_an_unknown_workspace_is_not_resolved(self, client, auth_headers):
        response = client.get(
            "/api/v1/documents?workspace_id=no-such-workspace", headers=auth_headers
        )
        # Never fall back to a workspace the caller did not ask for.
        assert response.status_code in (200, 400, 403, 404)
        if response.status_code == 200:
            assert response.json() == []


# ------------------------------------------------------------ configuration --
class TestConfigurationEdges:
    def _settings(self, **overrides):
        from app.core.config import Settings

        base = {
            "ENVIRONMENT": "development",
            "SECRET_KEY": "a-development-secret-key-long-enough-for-checks",
            "DATABASE_URL": "sqlite:///./test.db",
        }
        base.update(overrides)
        return Settings(_env_file=None, **base)

    def test_cors_origins_are_split_and_trimmed(self):
        settings = self._settings(
            CORS_ORIGINS="https://a.example.com, https://b.example.com",
            FRONTEND_URL="https://app.example.com",
        )
        assert settings.cors_origins[:2] == ["https://a.example.com", "https://b.example.com"]

    def test_blank_cors_entries_are_dropped(self):
        settings = self._settings(
            CORS_ORIGINS="https://a.example.com,,  ,", FRONTEND_URL="https://app.example.com"
        )
        assert "" not in settings.cors_origins
        assert settings.cors_origins[0] == "https://a.example.com"

    def test_the_frontend_url_is_always_allowed(self):
        settings = self._settings(
            CORS_ORIGINS="https://a.example.com", FRONTEND_URL="https://app.example.com"
        )
        # Otherwise the app the API ships with cannot call it.
        assert "https://app.example.com" in settings.cors_origins

    def test_origins_are_de_duplicated(self):
        settings = self._settings(
            CORS_ORIGINS="https://app.example.com,https://app.example.com",
            FRONTEND_URL="https://app.example.com",
        )
        assert settings.cors_origins.count("https://app.example.com") == 1

    def test_trusted_hosts_are_split(self):
        settings = self._settings(TRUSTED_HOSTS="a.example.com, b.example.com")
        assert set(settings.trusted_hosts) >= {"a.example.com", "b.example.com"}

    def test_redis_is_disabled_by_a_blank_url(self):
        assert self._settings(REDIS_URL="").redis_enabled is False

    def test_redis_is_enabled_by_a_url(self):
        assert self._settings(REDIS_URL="redis://localhost:6379").redis_enabled is True

    def test_production_is_determined_by_environment_only(self):
        assert self._settings(ENVIRONMENT="production", SECRET_KEY="x" * 48).is_production
        assert not self._settings(ENVIRONMENT="staging").is_production
        # DEBUG must not be able to turn production off.
        assert self._settings(
            ENVIRONMENT="production", SECRET_KEY="x" * 48, DEBUG=True
        ).is_production

    def test_upload_extensions_are_normalised(self):
        settings = self._settings(ALLOWED_UPLOAD_EXTENSIONS=".PDF, TXT ,.docx")
        allowed = settings.allowed_upload_extensions
        assert "pdf" in allowed and "txt" in allowed and "docx" in allowed
        assert not any(ext.startswith(".") for ext in allowed)

    def test_blocked_extensions_are_normalised(self):
        settings = self._settings(BLOCKED_UPLOAD_EXTENSIONS=".EXE, .bat")
        assert "exe" in settings.blocked_upload_extensions

    def test_upload_size_floors_at_one_megabyte(self):
        # setex-style guards elsewhere assume a non-zero cap.
        assert self._settings(MAX_UPLOAD_SIZE_MB=0).max_upload_bytes == 1024 * 1024

    def test_hsts_value_includes_the_configured_max_age(self):
        settings = self._settings(HSTS_MAX_AGE=31536000)
        assert "max-age=31536000" in settings.hsts_value

    def test_hsts_subdomain_and_preload_directives(self):
        settings = self._settings(
            HSTS_MAX_AGE=31536000, HSTS_INCLUDE_SUBDOMAINS=True, HSTS_PRELOAD=True
        )
        assert "includeSubDomains" in settings.hsts_value
        assert "preload" in settings.hsts_value

    def test_preload_is_omitted_when_disabled(self):
        settings = self._settings(HSTS_MAX_AGE=100, HSTS_PRELOAD=False)
        assert "preload" not in settings.hsts_value


# ----------------------------------------------------------------- logging ---
class TestStructuredLogging:
    def test_records_are_emitted_as_json(self):
        import json

        from app.core.logging import JSONFormatter

        record = logging.LogRecord(
            name="researchsphere.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="a message",
            args=(),
            exc_info=None,
        )
        payload = json.loads(JSONFormatter().format(record))
        assert payload["message"] == "a message"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "researchsphere.test"
        assert "timestamp" in payload

    def test_extra_fields_are_included(self):
        import json

        from app.core.logging import JSONFormatter

        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="m",
            args=(),
            exc_info=None,
        )
        record.user_id = "u-1"
        record.action = "document.upload"
        payload = json.loads(JSONFormatter().format(record))
        assert payload["user_id"] == "u-1"
        assert payload["action"] == "document.upload"

    def test_exceptions_are_serialised(self):
        import json
        import sys

        from app.core.logging import JSONFormatter

        try:
            raise ValueError("failure detail")
        except ValueError:
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="m",
            args=(),
            exc_info=exc_info,
        )
        payload = json.loads(JSONFormatter().format(record))
        # A traceback that breaks the JSON frame is useless to a log shipper.
        assert "ValueError" in json.dumps(payload)

    def test_a_message_with_newlines_stays_one_json_object(self):
        import json

        from app.core.logging import JSONFormatter

        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="line one\nline two",
            args=(),
            exc_info=None,
        )
        rendered = JSONFormatter().format(record)
        assert len(rendered.splitlines()) == 1
        assert json.loads(rendered)["message"] == "line one\nline two"


# ---------------------------------------------------------- error tracking ---
class TestErrorTrackerBackends:
    def test_an_unknown_backend_falls_back_to_logging(self):
        from app.core import tracking

        tracking.reset_tracker()
        with patch("app.core.tracking.settings") as configured:
            configured.ERROR_TRACKING_DSN = "unsupported://somewhere"
            tracker = tracking.get_tracker()
        # An unusable DSN must not stop errors being recorded at all.
        assert tracker is not None
        tracking.reset_tracker()

    def test_the_tracker_is_cached(self):
        from app.core import tracking

        tracking.reset_tracker()
        assert tracking.get_tracker() is tracking.get_tracker()

    def test_reset_clears_the_cache(self):
        from app.core import tracking

        tracking.reset_tracker()
        first = tracking.get_tracker()
        tracking.reset_tracker()
        assert tracking.get_tracker() is not first

    def test_capture_survives_a_failing_backend(self):
        from app.core import tracking

        tracking.reset_tracker()
        broken = MagicMock()
        broken.capture.side_effect = RuntimeError("tracker is down")
        with patch.object(tracking, "get_tracker", return_value=broken):
            # Reporting an error must never become a second error.
            tracking.capture_exception(ValueError("original"))


# --------------------------------------------------------------- RAG edges ---
class TestRagBoundaries:
    def test_the_embedding_model_is_loaded_once(self):
        from app.rag import embeddings

        (
            embeddings.get_embedding_model.cache_clear()
            if hasattr(embeddings.get_embedding_model, "cache_clear")
            else None
        )

        with patch("sentence_transformers.SentenceTransformer") as constructor:
            constructor.return_value = MagicMock()
            first = embeddings.get_embedding_model()
            second = embeddings.get_embedding_model()
        # Loading the model per request would add seconds to every query.
        assert first is second

    def test_the_qdrant_client_is_reused(self):
        from app.rag import vector_store

        with (
            patch.object(vector_store, "_client", None),
            patch("qdrant_client.QdrantClient") as constructor,
        ):
            constructor.return_value = MagicMock()
            first = vector_store.get_qdrant_client()
            second = vector_store.get_qdrant_client()
        assert first is second

    def test_a_qdrant_outage_surfaces_rather_than_returning_empty_results(self):
        from app.rag import vector_store

        failing = MagicMock()
        failing.search.side_effect = OSError("connection refused")
        with patch.object(vector_store, "get_qdrant_client", return_value=failing):
            # Swallowing this would answer "no relevant documents" during an
            # outage, which reads as an empty index rather than a failure.
            with pytest.raises(OSError, match="connection refused"):
                vector_store.search_similar([0.1] * 384, workspace_id="ws-1")

    def test_extraction_of_an_empty_file_yields_empty_text(self, tmp_path):
        from app.rag.document_processor import extract_text

        path = tmp_path / "empty.txt"
        path.write_text("", encoding="utf-8")
        assert extract_text(str(path), "txt") == ""

    def test_a_missing_file_raises(self, tmp_path):
        from app.rag.document_processor import extract_text

        with pytest.raises((FileNotFoundError, ValueError, OSError)):
            extract_text(str(tmp_path / "absent.txt"), "txt")

    def test_extension_matching_is_case_insensitive(self, tmp_path):
        from app.rag.document_processor import extract_text

        path = tmp_path / "upper.TXT"
        path.write_text("content", encoding="utf-8")
        assert extract_text(str(path), "TXT") == "content"
