"""
Infrastructure layer: Redis accessor, token revocation store, the config
validation CLI and the database engine fallback.

The theme throughout is degradation. Redis is optional, so the assertions are
mostly about what happens when it is missing or fails mid-operation -- that is
the behaviour that decides whether an outage is a blip or an incident.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from app.core import redis_client, token_store
from app.core.config import Settings, validate_configuration

pytestmark = pytest.mark.unit


# ------------------------------------------------------------ redis client --
class TestRedisAccessor:
    def test_unconfigured_redis_yields_no_client(self):
        redis_client.reset()
        original = redis_client.settings.REDIS_URL
        redis_client.settings.REDIS_URL = ""
        try:
            assert redis_client.is_configured() is False
            assert redis_client.get_redis() is None
        finally:
            redis_client.settings.REDIS_URL = original
            redis_client.reset()

    def test_injected_client_is_returned(self, fake_redis):
        assert redis_client.get_redis() is fake_redis

    def test_connection_failure_returns_none_rather_than_raising(self):
        redis_client.reset()
        original = redis_client.settings.REDIS_URL
        redis_client.settings.REDIS_URL = "redis://localhost:6379"
        try:
            with patch.object(
                redis_client._redis, "from_url", side_effect=OSError("connection refused")
            ):
                # A Redis outage must never propagate into a request.
                assert redis_client.get_redis() is None
        finally:
            redis_client.settings.REDIS_URL = original
            redis_client.reset()

    def test_failed_probe_is_not_retried_on_every_call(self):
        redis_client.reset()
        original = redis_client.settings.REDIS_URL
        redis_client.settings.REDIS_URL = "redis://localhost:6379"
        try:
            with patch.object(
                redis_client._redis, "from_url", side_effect=OSError("down")
            ) as connect:
                redis_client.get_redis()
                redis_client.get_redis()
                redis_client.get_redis()
            # Backoff: one attempt, then silence until the retry interval.
            assert connect.call_count == 1
        finally:
            redis_client.settings.REDIS_URL = original
            redis_client.reset()

    def test_force_bypasses_the_backoff_window(self):
        redis_client.reset()
        original = redis_client.settings.REDIS_URL
        redis_client.settings.REDIS_URL = "redis://localhost:6379"
        try:
            with patch.object(
                redis_client._redis, "from_url", side_effect=OSError("down")
            ) as connect:
                redis_client.get_redis()
                redis_client.get_redis(force=True)
            assert connect.call_count == 2
        finally:
            redis_client.settings.REDIS_URL = original
            redis_client.reset()

    def test_mid_operation_failure_drops_the_client(self, fake_redis):
        assert redis_client.get_redis() is fake_redis
        redis_client.mark_unavailable(OSError("broken pipe"))
        # The stale client must not be handed out again.
        assert redis_client.get_redis() is None

    def test_status_reports_configuration_and_connection(self, fake_redis):
        status = redis_client.status()
        assert status["configured"] is True
        assert status["connected"] is True

    def test_status_records_the_last_error(self):
        redis_client.reset()
        redis_client.mark_unavailable(OSError("some failure"))
        assert "some failure" in (redis_client.status()["last_error"] or "")
        redis_client.reset()

    def test_reset_clears_all_state(self, fake_redis):
        redis_client.reset()
        status = redis_client.status()
        assert status["connected"] is False
        assert status["last_error"] is None


# ------------------------------------------------------------- token store --
class TestTokenRevocation:
    def test_revoked_token_is_recognised(self, fake_redis):
        assert token_store.revoke_token("jti-1", exp=time.time() + 600) is True
        assert token_store.is_token_revoked("jti-1") is True

    def test_unknown_token_is_not_revoked(self, fake_redis):
        assert token_store.is_token_revoked("never-seen") is False

    def test_entry_carries_a_ttl_matching_the_token_lifetime(self, fake_redis):
        token_store.revoke_token("jti-ttl", exp=time.time() + 300)
        ttl = fake_redis.ttl(f"{token_store.REVOKED_PREFIX}jti-ttl")
        # Self-expiring entries are what keep the blacklist from growing forever.
        assert 0 < ttl <= 300

    def test_missing_expiry_falls_back_to_the_default_ttl(self, fake_redis):
        token_store.revoke_token("jti-noexp")
        ttl = fake_redis.ttl(f"{token_store.REVOKED_PREFIX}jti-noexp")
        assert ttl == pytest.approx(token_store.DEFAULT_TTL_SECONDS, rel=0.01)

    def test_already_expired_token_is_still_recorded_with_an_expiry(self, fake_redis):
        # setex rejects a non-positive TTL, so _ttl_from_exp floors at one
        # second. What matters is that the write succeeds and the entry is
        # bounded rather than persisted forever; the exact remainder is
        # sub-second, which redis reports as 0.
        assert token_store.revoke_token("jti-past", exp=time.time() - 500) is True
        ttl = fake_redis.ttl(f"{token_store.REVOKED_PREFIX}jti-past")
        assert ttl != -1, "entry was stored without an expiry"
        assert 0 <= ttl <= 1

    def test_datetime_expiry_is_accepted(self, fake_redis):
        from datetime import UTC, datetime, timedelta

        token_store.revoke_token("jti-dt", exp=datetime.now(UTC) + timedelta(seconds=120))
        assert 0 < fake_redis.ttl(f"{token_store.REVOKED_PREFIX}jti-dt") <= 120

    def test_naive_datetime_expiry_is_treated_as_utc(self, fake_redis):
        from datetime import UTC, datetime, timedelta

        naive = (datetime.now(UTC) + timedelta(seconds=120)).replace(tzinfo=None)
        token_store.revoke_token("jti-naive", exp=naive)
        assert fake_redis.ttl(f"{token_store.REVOKED_PREFIX}jti-naive") > 0

    def test_unparseable_expiry_falls_back_rather_than_raising(self, fake_redis):
        assert token_store.revoke_token("jti-junk", exp="not a timestamp") is True

    def test_empty_jti_is_ignored(self, fake_redis):
        assert token_store.revoke_token("") is False
        assert token_store.is_token_revoked("") is False

    def test_revocation_without_redis_reports_failure(self):
        redis_client.reset()
        with patch.object(token_store, "get_redis", return_value=None):
            assert token_store.revoke_token("jti-x", exp=time.time() + 60) is False

    def test_revocation_check_fails_open_without_redis(self):
        """A Redis outage must not lock every user out.

        Returning True here would reject every request carrying a valid token,
        turning a cache outage into a total authentication outage. Refresh
        tokens are unaffected because their state lives in the database.
        """
        with patch.object(token_store, "get_redis", return_value=None):
            assert token_store.is_token_revoked("anything") is False

    def test_check_fails_open_when_the_command_errors(self):
        client = MagicMock()
        client.exists.side_effect = OSError("connection reset")
        with patch.object(token_store, "get_redis", return_value=client):
            assert token_store.is_token_revoked("jti-1") is False

    def test_write_failure_is_reported_and_marks_redis_unavailable(self):
        client = MagicMock()
        client.setex.side_effect = OSError("connection reset")
        with patch.object(token_store, "get_redis", return_value=client), patch.object(
            token_store, "mark_unavailable"
        ) as marked:
            assert token_store.revoke_token("jti-1", exp=time.time() + 60) is False
        marked.assert_called_once()

    def test_bulk_revocation_counts_successes(self, fake_redis):
        count = token_store.revoke_many(["a", "b", "c"], exp=time.time() + 60)
        assert count == 3
        assert all(token_store.is_token_revoked(jti) for jti in ("a", "b", "c"))

    def test_bulk_revocation_skips_empty_ids(self, fake_redis):
        assert token_store.revoke_many(["a", "", "c"], exp=time.time() + 60) == 2

    def test_blacklist_size_counts_only_revocation_keys(self, fake_redis):
        fake_redis.set("unrelated:key", "1")
        token_store.revoke_token("size-1", exp=time.time() + 60)
        token_store.revoke_token("size-2", exp=time.time() + 60)
        assert token_store.blacklist_size() == 2

    def test_blacklist_size_is_none_without_redis(self):
        with patch.object(token_store, "get_redis", return_value=None):
            # None means "unknown", which is not the same as zero.
            assert token_store.blacklist_size() is None

    def test_blacklist_size_is_none_when_the_scan_fails(self):
        client = MagicMock()
        client.scan_iter.side_effect = OSError("scan failed")
        with patch.object(token_store, "get_redis", return_value=client), patch.object(
            token_store, "mark_unavailable"
        ):
            assert token_store.blacklist_size() is None


# ------------------------------------------------- configuration validation --
def _settings(**overrides) -> Settings:
    """Build Settings without reading the developer's local .env."""
    base = {
        "ENVIRONMENT": "development",
        "SECRET_KEY": "a-development-secret-key-long-enough-for-checks",
        "DATABASE_URL": "sqlite:///./test.db",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class TestConfigurationValidation:
    def test_a_sane_development_config_has_no_errors(self):
        assert validate_configuration(_settings())["errors"] == []

    def test_production_rejects_a_weak_secret(self):
        report = validate_configuration(
            _settings(ENVIRONMENT="production", SECRET_KEY="short")
        )
        assert any("SECRET_KEY" in error for error in report["errors"])

    def test_production_rejects_wildcard_cors(self):
        report = validate_configuration(
            _settings(
                ENVIRONMENT="production",
                SECRET_KEY="x" * 48,
                CORS_ORIGINS="*",
                DATABASE_URL="postgresql://u:p@db/rs",
            )
        )
        # "*" with credentials enabled lets any origin drive an authenticated
        # session, so it must be fatal rather than a warning.
        assert any("CORS" in error.upper() for error in report["errors"])

    def test_production_rejects_a_sqlite_database(self):
        report = validate_configuration(
            _settings(
                ENVIRONMENT="production",
                SECRET_KEY="x" * 48,
                DATABASE_URL="sqlite:///./researchsphere.db",
            )
        )
        assert any("sqlite" in error.lower() for error in report["errors"])

    def test_production_rejects_debug_mode(self):
        report = validate_configuration(
            _settings(
                ENVIRONMENT="production",
                SECRET_KEY="x" * 48,
                DEBUG=True,
                DATABASE_URL="postgresql://u:p@db/rs",
            )
        )
        assert any("DEBUG" in error for error in report["errors"])

    def test_development_reports_the_same_issues_as_warnings(self):
        report = validate_configuration(_settings(SECRET_KEY="short"))
        # Development must stay startable; the problem is still surfaced.
        assert report["errors"] == []
        assert report["warnings"]

    def test_report_always_has_both_keys(self):
        report = validate_configuration(_settings())
        assert set(report) == {"errors", "warnings"}


class TestConfigValidationCLI:
    def test_valid_configuration_exits_zero(self, capsys):
        from app.cli import validate_config

        with patch.object(
            validate_config, "validate_configuration", return_value={"errors": [], "warnings": []}
        ):
            assert validate_config.main() == 0
        assert "All configuration checks passed." in capsys.readouterr().out

    def test_errors_exit_non_zero(self, capsys):
        from app.cli import validate_config

        with patch.object(
            validate_config,
            "validate_configuration",
            return_value={"errors": ["SECRET_KEY is too short"], "warnings": []},
        ):
            # The container entrypoint relies on this exit code to fail fast.
            assert validate_config.main() == 1
        output = capsys.readouterr().out
        assert "[ERROR] SECRET_KEY is too short" in output
        assert "FAILED: 1 error(s)" in output

    def test_warnings_alone_still_exit_zero(self, capsys):
        from app.cli import validate_config

        with patch.object(
            validate_config,
            "validate_configuration",
            return_value={"errors": [], "warnings": ["Redis is not configured"]},
        ):
            assert validate_config.main() == 0
        output = capsys.readouterr().out
        assert "[WARN ] Redis is not configured" in output
        assert "OK: 0 errors, 1 warning(s)." in output

    def test_environment_and_version_are_printed(self, capsys):
        from app.cli import validate_config

        with patch.object(
            validate_config, "validate_configuration", return_value={"errors": [], "warnings": []}
        ):
            validate_config.main()
        output = capsys.readouterr().out
        assert "Environment :" in output
        assert "Application :" in output

    def test_module_is_runnable_as_a_script(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "app.cli.validate_config"],
            capture_output=True,
            text=True,
        )
        # The entrypoint invokes it exactly this way.
        assert result.returncode in (0, 1)
        assert "Environment :" in result.stdout


# --------------------------------------------------------------- database ---
class TestDatabaseEngine:
    def test_get_db_yields_a_session_and_closes_it(self):
        from app.core.database import get_db

        generator = get_db()
        session = next(generator)
        with patch.object(session, "close") as close:
            generator.close()
        # Leaking sessions exhausts the connection pool under load.
        close.assert_called_once()

    def test_session_is_closed_even_when_the_caller_raises(self):
        from app.core.database import get_db

        generator = get_db()
        session = next(generator)
        with patch.object(session, "close") as close:
            with pytest.raises(RuntimeError):
                generator.throw(RuntimeError("handler blew up"))
        close.assert_called_once()

    def test_development_falls_back_to_sqlite_when_the_database_is_down(self):
        """The fallback is a development convenience and must stay working."""
        import importlib

        import app.core.database as database_module

        real_create_engine = database_module.create_engine
        calls = []

        def _create_engine(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                raise OSError("connection refused")
            return real_create_engine(url, **kwargs)

        with patch("sqlalchemy.create_engine", _create_engine), patch(
            "app.core.config.settings.ENVIRONMENT", "development"
        ):
            reloaded = importlib.reload(database_module)

        try:
            assert len(calls) == 2
            assert calls[1].startswith("sqlite:")
            assert reloaded.engine.url.get_backend_name() == "sqlite"
        finally:
            importlib.reload(database_module)

    def test_production_refuses_the_sqlite_fallback(self):
        """A database outage in production must not silently swap in SQLite.

        The fallback presents an empty local file as a healthy database, so the
        service would start, answer requests and lose every write.
        """
        import importlib

        import app.core.database as database_module

        with patch("sqlalchemy.create_engine", side_effect=OSError("connection refused")), patch(
            "app.core.config.settings.ENVIRONMENT", "production"
        ):
            with pytest.raises(OSError):
                importlib.reload(database_module)

        # Restore the module the rest of the session shares.
        importlib.reload(database_module)

    def test_reloaded_engine_still_serves_the_test_database(self):
        from app.core.database import engine

        assert engine.url.get_backend_name() == "sqlite"
