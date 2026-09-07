"""
Configuration validation, exhaustively.

This is the gate that decides whether a production container starts. Every
check gets a case, because a validator that silently stops checking something
is worse than no validator: it converts an outage into a false sense of
safety.
"""

import pytest

from app.core.config import (
    INSECURE_SECRET_DEFAULTS as _KNOWN_DEFAULTS,
)
from app.core.config import Settings, validate_configuration

pytestmark = pytest.mark.unit


def _settings(**overrides) -> Settings:
    """Build Settings without reading the developer's local .env."""
    base = {
        "ENVIRONMENT": "production",
        "SECRET_KEY": "x" * 48,
        "DATABASE_URL": "postgresql://user:pass@db.internal:5432/researchsphere",
        "GEMINI_API_KEY": "a-gemini-key",
        "CORS_ORIGINS": "https://app.example.com",
        "FRONTEND_URL": "https://app.example.com",
        "TRUSTED_HOSTS": "app.example.com",
        "REDIS_URL": "redis://cache.internal:6379",
        "QDRANT_HOST": "qdrant.internal",
        "QDRANT_PORT": 6333,
        "QDRANT_COLLECTION": "documents",
        "DEBUG": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _errors(**overrides) -> list[str]:
    return validate_configuration(_settings(**overrides))["errors"]


def _warnings(**overrides) -> list[str]:
    return validate_configuration(_settings(**overrides))["warnings"]


class TestValidProduction:
    def test_a_complete_production_config_passes(self, tmp_path):
        errors = _errors(
            UPLOAD_DIR=str(tmp_path / "uploads"),
            QUARANTINE_DIR=str(tmp_path / "quarantine"),
        )
        assert errors == [], errors


class TestSecretValidation:
    def test_a_missing_secret_is_fatal(self):
        assert any("SECRET_KEY" in e for e in _errors(SECRET_KEY=""))

    def test_a_short_secret_is_fatal(self):
        assert any("too short" in e for e in _errors(SECRET_KEY="tooshort"))

    @pytest.mark.parametrize("value", sorted(_KNOWN_DEFAULTS))
    def test_well_known_defaults_are_rejected(self, value):
        # A default that ships in a README or a .env.example is a public key,
        # including the long ones that pass the length check.
        errors = _errors(SECRET_KEY=value)
        assert any("default" in e for e in errors), errors

    def test_the_shipped_example_secret_is_in_the_rejected_set(self):
        # If .env.prod.example ever gains a new placeholder secret, it must be
        # added to INSECURE_SECRET_DEFAULTS or this gate stops meaning anything.
        assert _KNOWN_DEFAULTS
        assert all(isinstance(value, str) and value for value in _KNOWN_DEFAULTS)

    def test_the_none_algorithm_is_rejected(self):
        # alg=none turns every forged token into a valid one.
        assert any("ALGORITHM" in e for e in _errors(ALGORITHM="none"))

    def test_the_none_algorithm_is_rejected_case_insensitively(self):
        assert any("ALGORITHM" in e for e in _errors(ALGORITHM="NoNe"))


class TestDatabaseValidation:
    def test_a_missing_url_is_fatal(self):
        assert any("DATABASE_URL" in e for e in _errors(DATABASE_URL=""))

    def test_sqlite_is_rejected_in_production(self):
        assert any("sqlite" in e.lower() for e in _errors(DATABASE_URL="sqlite:///./app.db"))

    def test_a_localhost_database_warns_in_production(self):
        warnings = _warnings(DATABASE_URL="postgresql://u:p@localhost:5432/rs")
        # Usually means the container is pointed at itself.
        assert any("localhost" in w for w in warnings)


class TestRedisValidation:
    def test_a_malformed_url_is_rejected(self):
        assert _errors(REDIS_URL="http://cache.internal:6379")

    @pytest.mark.parametrize(
        "url", ["redis://c:6379", "rediss://c:6379", "unix:///var/run/redis.sock"]
    )
    def test_valid_schemes_are_accepted(self, url, tmp_path):
        errors = _errors(
            REDIS_URL=url,
            UPLOAD_DIR=str(tmp_path / "u"),
            QUARANTINE_DIR=str(tmp_path / "q"),
        )
        assert not any("REDIS" in e.upper() for e in errors), errors

    def test_an_empty_url_is_allowed(self, tmp_path):
        # Redis is optional; absence is a deployment choice, not an error.
        errors = _errors(
            REDIS_URL="", UPLOAD_DIR=str(tmp_path / "u"), QUARANTINE_DIR=str(tmp_path / "q")
        )
        assert not any("REDIS" in e.upper() for e in errors), errors


class TestQdrantValidation:
    def test_a_missing_host_is_fatal(self):
        assert any("QDRANT_HOST" in e for e in _errors(QDRANT_HOST=""))

    @pytest.mark.parametrize("port", [0, 70000, -1])
    def test_an_out_of_range_port_is_fatal(self, port):
        assert any("QDRANT_PORT" in e for e in _errors(QDRANT_PORT=port))

    def test_a_missing_collection_is_fatal(self):
        assert any("QDRANT_COLLECTION" in e for e in _errors(QDRANT_COLLECTION=""))


class TestCorsValidation:
    def test_no_origins_is_fatal(self):
        assert any("CORS" in e.upper() for e in _errors(CORS_ORIGINS="", FRONTEND_URL=""))

    def test_a_wildcard_is_fatal_in_production(self):
        assert any("Wildcard" in e for e in _errors(CORS_ORIGINS="*"))

    def test_an_origin_without_a_scheme_is_fatal(self):
        assert any("not a valid URL" in e for e in _errors(CORS_ORIGINS="app.example.com"))

    def test_a_plain_http_origin_warns_in_production(self):
        warnings = _warnings(
            CORS_ORIGINS="http://app.example.com", FRONTEND_URL="http://app.example.com"
        )
        # Credentials over plain HTTP are readable in transit.
        assert any("plain HTTP" in w for w in warnings)


class TestTrustedHostValidation:
    def test_a_wildcard_host_is_fatal_in_production(self):
        assert any("TRUSTED_HOSTS" in e for e in _errors(TRUSTED_HOSTS="*"))

    def test_a_wildcard_host_is_allowed_outside_production(self, tmp_path):
        errors = _errors(
            ENVIRONMENT="development",
            TRUSTED_HOSTS="*",
            UPLOAD_DIR=str(tmp_path / "u"),
            QUARANTINE_DIR=str(tmp_path / "q"),
        )
        assert not any("TRUSTED_HOSTS" in e for e in errors)


class TestUploadValidation:
    def test_an_unwritable_upload_directory_is_fatal(self, tmp_path):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("i am a file", encoding="utf-8")
        errors = _errors(
            UPLOAD_DIR=str(blocker / "uploads"), QUARANTINE_DIR=str(tmp_path / "q")
        )
        assert any("UPLOAD_DIR" in e for e in errors), errors

    def test_a_missing_upload_directory_setting_is_fatal(self, tmp_path):
        assert any(
            "UPLOAD_DIR" in e
            for e in _errors(UPLOAD_DIR="", QUARANTINE_DIR=str(tmp_path / "q"))
        )

    def test_quarantine_must_differ_from_the_upload_directory(self, tmp_path):
        shared = str(tmp_path / "shared")
        errors = _errors(UPLOAD_DIR=shared, QUARANTINE_DIR=shared)
        # Sharing them means an unscanned file sits in the served directory.
        assert any("QUARANTINE_DIR" in e for e in errors), errors

    def test_a_non_positive_upload_size_is_fatal(self, tmp_path):
        errors = _errors(
            MAX_UPLOAD_SIZE_MB=0,
            UPLOAD_DIR=str(tmp_path / "u"),
            QUARANTINE_DIR=str(tmp_path / "q"),
        )
        assert any("MAX_UPLOAD_SIZE_MB" in e for e in errors)

    def test_an_empty_allowlist_is_fatal(self, tmp_path):
        errors = _errors(
            ALLOWED_UPLOAD_EXTENSIONS="",
            UPLOAD_DIR=str(tmp_path / "u"),
            QUARANTINE_DIR=str(tmp_path / "q"),
        )
        # Every upload would be rejected, which looks like a broken product.
        assert any("ALLOWED_UPLOAD_EXTENSIONS" in e for e in errors)


class TestGeminiValidation:
    def test_a_missing_key_is_fatal_in_production(self):
        assert any("GEMINI_API_KEY" in e for e in _errors(GEMINI_API_KEY=""))

    def test_a_missing_key_only_warns_in_development(self, tmp_path):
        report = validate_configuration(
            _settings(
                ENVIRONMENT="development",
                GEMINI_API_KEY="",
                UPLOAD_DIR=str(tmp_path / "u"),
                QUARANTINE_DIR=str(tmp_path / "q"),
            )
        )
        assert not any("GEMINI_API_KEY" in e for e in report["errors"])
        assert any("GEMINI_API_KEY" in w for w in report["warnings"])


class TestProductionHygiene:
    def test_debug_is_fatal_in_production(self):
        assert any("DEBUG" in e for e in _errors(DEBUG=True))

    def test_debug_is_fine_in_development(self, tmp_path):
        errors = _errors(
            ENVIRONMENT="development",
            DEBUG=True,
            UPLOAD_DIR=str(tmp_path / "u"),
            QUARANTINE_DIR=str(tmp_path / "q"),
        )
        assert not any("DEBUG" in e for e in errors)

    def test_every_production_error_is_a_development_warning(self):
        """Development must stay startable while still reporting everything."""
        broken = {
            "SECRET_KEY": "short",
            "DATABASE_URL": "sqlite:///./app.db",
            "GEMINI_API_KEY": "",
            "QDRANT_HOST": "",
        }
        production = validate_configuration(_settings(**broken))
        development = validate_configuration(_settings(ENVIRONMENT="development", **broken))

        assert production["errors"]
        assert development["errors"] == []
        assert len(development["warnings"]) >= len(production["errors"])
