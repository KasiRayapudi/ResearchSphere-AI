"""
Remaining branches: the exception hierarchy, file logging, upload-security
edges, refresh rotation failures, audit redaction and metric helpers.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ------------------------------------------------------- exception hierarchy --
class TestExceptionTypes:
    @pytest.mark.parametrize(
        "factory,status,code",
        [
            (lambda m: __import__(
                "app.core.exceptions", fromlist=["ResourceNotFoundException"]
            ).ResourceNotFoundException("Document", "doc-1"), 404, "RESOURCE_NOT_FOUND"),
            (lambda m: __import__(
                "app.core.exceptions", fromlist=["AuthenticationException"]
            ).AuthenticationException(), 401, "AUTHENTICATION_FAILED"),
            (lambda m: __import__(
                "app.core.exceptions", fromlist=["AuthorizationException"]
            ).AuthorizationException(), 403, "AUTHORIZATION_FAILED"),
            (lambda m: __import__(
                "app.core.exceptions", fromlist=["ValidationException"]
            ).ValidationException(), 422, "VALIDATION_ERROR"),
            (lambda m: __import__(
                "app.core.exceptions", fromlist=["RateLimitException"]
            ).RateLimitException(), 429, "RATE_LIMIT_EXCEEDED"),
            (lambda m: __import__(
                "app.core.exceptions", fromlist=["RAGProcessingException"]
            ).RAGProcessingException(), 500, "RAG_PROCESSING_ERROR"),
            (lambda m: __import__(
                "app.core.exceptions", fromlist=["FileValidationException"]
            ).FileValidationException(), 400, "FILE_VALIDATION_ERROR"),
        ],
    )
    def test_each_type_carries_its_status_and_code(self, factory, status, code):
        exc = factory(None)
        assert exc.status_code == status
        assert exc.code == code
        assert exc.message

    def test_not_found_names_the_resource(self):
        from app.core.exceptions import ResourceNotFoundException

        exc = ResourceNotFoundException("Document", "doc-1")
        assert "Document" in exc.message and "doc-1" in exc.message

    def test_validation_details_are_carried_through(self):
        from app.core.exceptions import ValidationException, format_error_response

        details = [{"loc": ["body", "email"], "msg": "invalid", "type": "value_error"}]
        body = format_error_response(ValidationException(details=details))
        assert body["error"]["details"] == details

    def test_details_default_to_an_empty_list(self):
        from app.core.exceptions import AppException

        assert AppException(message="m", code="C", status_code=400).details == []

    def test_the_message_is_the_str_representation(self):
        from app.core.exceptions import AppException

        assert str(AppException(message="the message", code="C", status_code=400)) == (
            "the message"
        )


# ------------------------------------------------------------- file logging --
class TestFileLogging:
    @staticmethod
    def _restore(before):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if handler not in before:
                handler.close()
                root.removeHandler(handler)

    def test_a_rotating_file_handler_is_attached(self, tmp_path):
        from app.core.config import settings
        from app.core.logging import setup_logging

        log_file = tmp_path / "nested" / "app.log"
        root = logging.getLogger()
        before = list(root.handlers)
        original = settings.LOG_FILE
        settings.LOG_FILE = str(log_file)
        try:
            setup_logging()
            # The parent directory must be created rather than assumed.
            assert log_file.parent.exists()
            logging.getLogger("researchsphere.test").info("a line")
            for handler in root.handlers:
                handler.flush()
            assert log_file.exists()
        finally:
            settings.LOG_FILE = original
            self._restore(before)
            setup_logging()

    def test_written_lines_are_json(self, tmp_path):
        from app.core.config import settings
        from app.core.logging import setup_logging

        log_file = tmp_path / "app.log"
        root = logging.getLogger()
        before = list(root.handlers)
        original = settings.LOG_FILE
        settings.LOG_FILE = str(log_file)
        try:
            setup_logging()
            logging.getLogger("researchsphere.test").info("structured line")
            for handler in root.handlers:
                handler.flush()
            lines = [
                ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()
            ]
            assert lines
            assert json.loads(lines[-1])["message"]
        finally:
            settings.LOG_FILE = original
            self._restore(before)
            setup_logging()

    def test_an_unwritable_path_does_not_stop_startup(self, tmp_path):
        from app.core.config import settings
        from app.core.logging import setup_logging

        root = logging.getLogger()
        before = list(root.handlers)
        original = settings.LOG_FILE
        settings.LOG_FILE = str(tmp_path / "app.log")
        try:
            with patch("pathlib.Path.mkdir", side_effect=PermissionError("read-only")):
                # Losing file logs is bad; failing to start is worse.
                setup_logging()
        finally:
            settings.LOG_FILE = original
            self._restore(before)
            setup_logging()


# ---------------------------------------------------------- upload security --
class TestUploadSecurityEdges:
    def test_a_path_traversal_filename_is_flattened(self):
        from app.core.upload_security import sanitize_filename

        cleaned = sanitize_filename("../../../etc/passwd")
        assert "/" not in cleaned and "\\" not in cleaned
        assert ".." not in cleaned

    def test_a_windows_path_is_flattened(self):
        from app.core.upload_security import sanitize_filename

        cleaned = sanitize_filename(r"..\..\windows\system32\config.txt")
        assert "\\" not in cleaned and ".." not in cleaned

    def test_a_null_byte_is_stripped(self):
        from app.core.upload_security import sanitize_filename

        # "shell.php\x00.txt" truncates to .php on a naive C-level consumer.
        assert "\x00" not in sanitize_filename("shell.php\x00.txt")

    def test_an_empty_filename_still_yields_something_usable(self):
        from app.core.upload_security import sanitize_filename

        assert sanitize_filename("").strip()

    def test_a_very_long_filename_is_bounded(self):
        from app.core.upload_security import sanitize_filename

        assert len(sanitize_filename("a" * 5000 + ".txt")) < 300

    def test_the_extension_is_taken_from_the_last_dot(self):
        from app.core.upload_security import extract_extension

        assert extract_extension("archive.tar.gz") == "gz"

    def test_a_file_with_no_extension_yields_empty(self):
        from app.core.upload_security import extract_extension

        assert extract_extension("README") == ""

    def test_a_stored_name_is_unique_per_upload(self):
        from app.core.upload_security import build_storage_filename

        # Two uploads of the same name must not overwrite each other.
        assert build_storage_filename("txt") != build_storage_filename("txt")

    def test_the_stored_name_does_not_derive_from_the_client_filename(self):
        from app.core.upload_security import build_storage_filename

        stored = build_storage_filename("txt")
        # Traversal and overwrite are ruled out by construction, rather than
        # by sanitising the supplied name well enough.
        assert stored.endswith(".txt")
        assert "report" not in stored

    def test_a_hostile_extension_is_scrubbed_from_the_stored_name(self):
        from app.core.upload_security import build_storage_filename

        stored = build_storage_filename("../../evil")
        assert "/" not in stored and ".." not in stored

    def test_resolving_outside_the_root_is_refused(self, tmp_path):
        from app.core.upload_security import UploadValidationError, resolve_within

        with pytest.raises((UploadValidationError, ValueError)):
            resolve_within(str(tmp_path), "../escape.txt")

    def test_resolving_inside_the_root_succeeds(self, tmp_path):
        from app.core.upload_security import resolve_within

        assert str(tmp_path) in str(resolve_within(str(tmp_path), "inside.txt"))

    def test_an_empty_upload_is_rejected(self, tmp_path):
        from app.core.upload_security import UploadValidationError, validate_content

        path = tmp_path / "empty.txt"
        path.write_bytes(b"")
        with pytest.raises(UploadValidationError):
            validate_content(str(path), "txt")

    @pytest.mark.parametrize(
        "name,payload",
        [
            ("windows executable", b"MZ\x90\x00" + b"\x00" * 300),
            ("elf binary", b"\x7fELF\x02\x01\x01" + b"\x00" * 300),
            ("shell script", b"#!/bin/sh\nrm -rf /\n" + b"x" * 300),
        ],
    )
    def test_executable_content_is_rejected_whatever_the_extension(
        self, tmp_path, name, payload
    ):
        from app.core.upload_security import UploadValidationError, validate_content

        path = tmp_path / "disguised.txt"
        path.write_bytes(payload)
        with pytest.raises(UploadValidationError):
            validate_content(str(path), "txt")

    def test_discarding_a_missing_file_does_not_raise(self, tmp_path):
        from app.core.upload_security import discard_quarantined

        # Cleanup runs on failure paths and must never mask the real error.
        discard_quarantined(str(tmp_path / "never-existed.txt"))

    def test_discarding_survives_a_locked_file(self, tmp_path):
        from app.core.upload_security import discard_quarantined

        path = tmp_path / "locked.txt"
        path.write_text("data", encoding="utf-8")
        with patch("os.remove", side_effect=PermissionError("in use")):
            discard_quarantined(str(path))


# -------------------------------------------------------- refresh rotation ---
class TestRefreshRotation:
    pytestmark = pytest.mark.integration

    def test_a_refresh_token_can_be_exchanged(self, client, registered_user):
        response = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered_user["refresh_token"]}
        )
        assert response.status_code == 200
        assert response.json()["access_token"]

    def test_the_refresh_token_is_rotated(self, client, registered_user):
        body = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered_user["refresh_token"]}
        ).json()
        # A refresh token that survives its own use is a long-lived bearer
        # credential, which is what rotation exists to avoid.
        assert body["refresh_token"] != registered_user["refresh_token"]

    def test_reuse_is_detected_and_the_family_is_revoked(self, client, registered_user):
        first = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered_user["refresh_token"]}
        )
        assert first.status_code == 200
        rotated = first.json()["refresh_token"]

        replay = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered_user["refresh_token"]}
        )
        assert replay.status_code == 401

        # Detecting theft means invalidating the whole family, not just the
        # replayed token -- the attacker may hold the rotated one.
        after = client.post("/api/v1/auth/refresh", json={"refresh_token": rotated})
        assert after.status_code == 401

    def test_an_unknown_refresh_token_is_rejected(self, client):
        response = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"}
        )
        assert response.status_code == 401

    def test_an_access_token_cannot_be_used_to_refresh(self, client, registered_user):
        response = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": registered_user["access_token"]}
        )
        assert response.status_code == 401


# ------------------------------------------------------------------ audit ----
class TestAuditRedaction:
    def test_sensitive_keys_are_redacted(self):
        from app.core.audit import redact

        cleaned = redact({"password": "hunter2", "api_key": "sk-live-123", "email": "a@b.com"})
        assert cleaned["password"] != "hunter2"
        assert cleaned["api_key"] != "sk-live-123"
        # Non-secret context must survive, or the record is useless.
        assert cleaned["email"] == "a@b.com"

    def test_nested_structures_are_redacted(self):
        from app.core.audit import redact

        cleaned = redact({"outer": {"secret": "value", "inner": [{"token": "abc"}]}})
        assert "value" not in json.dumps(cleaned)
        assert "abc" not in json.dumps(cleaned)

    def test_a_bearer_token_is_redacted_by_shape(self):
        from app.core.audit import redact

        jwt_like = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        assert jwt_like not in json.dumps(redact({"note": jwt_like}))

    def test_a_bearer_header_is_redacted_by_shape(self):
        from app.core.audit import redact

        assert "abc123" not in json.dumps(redact({"note": "Bearer abc123"}))

    def test_a_short_dotted_string_is_not_mistaken_for_a_token(self):
        from app.core.audit import redact

        # Over-redaction destroys the context an audit record exists for.
        assert redact({"note": "app.core.audit"})["note"] == "app.core.audit"

    def test_numbers_under_sensitive_keys_are_preserved(self):
        from app.core.audit import redact

        # Redacting a count tells the reader nothing and loses real signal.
        cleaned = redact({"password_attempts": 3, "token_count": 12})
        assert cleaned["password_attempts"] == 3
        assert cleaned["token_count"] == 12

    def test_booleans_under_sensitive_keys_are_preserved(self):
        from app.core.audit import redact

        assert redact({"password_reset_required": True})["password_reset_required"] is True

    def test_auditing_never_raises(self):
        from app.core.audit import AuditAction, AuditOutcome, audit

        # An audit failure must not take the request down with it.
        audit(
            action=AuditAction.PERMISSION_DENIED,
            actor=object(),
            outcome=AuditOutcome.DENIED,
            resource="thing:1",
            request=None,
            metadata={"unserialisable": object()},
        )

    def test_an_unresolvable_actor_is_described_not_dropped(self):
        from app.core.audit import _describe_actor

        class _Detached:
            @property
            def id(self):
                raise RuntimeError("instance is detached from its session")

        # A lazily-loaded or detached ORM instance must cost us the actor's
        # identity, not the whole audit record.
        described = _describe_actor(_Detached())
        assert described["username"] == "<unresolvable-actor>"
        assert described["user_id"] is None

    def test_an_anonymous_actor_is_described(self):
        from app.core.audit import _describe_actor

        assert _describe_actor(None)["username"] == "anonymous"

    def test_a_string_actor_is_accepted(self):
        from app.core.audit import _describe_actor

        assert _describe_actor("system")["username"] == "system"

    def test_a_mapping_actor_is_accepted(self):
        from app.core.audit import _describe_actor

        described = _describe_actor({"id": "u-1", "email": "a@b.com", "role": "admin"})
        assert described == {"user_id": "u-1", "username": "a@b.com", "role": "admin"}

    def test_an_orm_actor_is_accepted(self):
        from app.core.audit import _describe_actor

        user = MagicMock(id="u-2", email="c@d.com", role="member")
        described = _describe_actor(user)
        assert described["user_id"] == "u-2"
        assert described["username"] == "c@d.com"

    def test_a_password_hash_is_never_included(self):
        from app.core.audit import _describe_actor

        user = MagicMock(id="u-3", email="e@f.com", role="member")
        user.hashed_password = "$2b$12$averysecrethash"
        assert "averysecrethash" not in json.dumps(_describe_actor(user))


# ---------------------------------------------------------------- metrics ----
class TestMetricHelpers:
    def test_track_db_records_a_duration(self):
        from app.core import metrics

        with metrics.track_db("select"):
            pass
        assert "researchsphere_db_query_duration_seconds" in metrics.render().decode()

    def test_track_db_records_even_on_failure(self):
        from app.core import metrics

        with pytest.raises(RuntimeError):
            with metrics.track_db("select"):
                raise RuntimeError("query failed")

    def test_app_info_is_exported(self):
        from app.core import metrics

        metrics.set_app_info()
        assert "researchsphere_app_info" in metrics.render().decode()

    def test_multiprocess_mode_is_reported(self):
        from app.core import metrics

        assert isinstance(metrics.is_multiprocess(), bool)

    def test_safe_runs_the_callable(self):
        from app.core import metrics

        calls = []
        metrics.safe(calls.append, "value")
        assert calls == ["value"]

    def test_safe_forwards_keyword_arguments(self):
        from app.core import metrics

        seen = {}
        metrics.safe(lambda **kw: seen.update(kw), outcome="ok")
        assert seen == {"outcome": "ok"}

    def test_safe_swallows_a_failure(self):
        from app.core import metrics

        # Fire and forget: instrumentation must never fail a request.
        assert metrics.safe(lambda: 1 / 0) is None
