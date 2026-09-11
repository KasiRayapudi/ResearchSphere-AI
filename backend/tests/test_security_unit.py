"""Unit tests for the security primitives: JWT, password policy, audit redaction."""

from datetime import timedelta

import pytest
from fastapi import HTTPException
from jose import jwt

from app.core.audit import REDACTED, AuditAction, AuditOutcome, redact
from app.core.config import Settings, validate_configuration
from app.core.password_policy import PasswordPolicyError, evaluate_password, validate_password
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    role_rank,
    verify_password,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------- passwords --
class TestPasswordHashing:
    def test_hash_is_not_reversible(self):
        digest = hash_password("Xk9#mQp2$vLw7")
        assert "Xk9#mQp2$vLw7" not in digest
        assert digest.startswith("$2b$")

    def test_verify_accepts_correct_password(self):
        assert verify_password("Xk9#mQp2$vLw7", hash_password("Xk9#mQp2$vLw7"))

    def test_verify_rejects_wrong_password(self):
        assert not verify_password("wrong", hash_password("Xk9#mQp2$vLw7"))

    def test_salted_hashes_differ(self):
        assert hash_password("same") != hash_password("same")

    def test_verify_survives_malformed_hash(self):
        assert not verify_password("anything", "not-a-bcrypt-hash")


class TestPasswordPolicy:
    @pytest.mark.parametrize(
        "password,reason",
        [
            ("Sh0rt!", "too short"),
            ("alllowercase123!", "no uppercase"),
            ("ALLUPPERCASE123!", "no lowercase"),
            ("NoDigitsHere!!", "no digit"),
            ("NoSpecialChars123", "no special character"),
            ("Password123!", "common password"),
            ("aaaBBBccc111!!!", "repeated characters"),
            ("Abcd1234!xyz", "predictable sequence"),
        ],
    )
    def test_rejects_weak_passwords(self, password, reason):
        assert not evaluate_password(password).is_valid, reason

    def test_accepts_strong_password(self):
        result = evaluate_password("Tr0ub4dor&Kestrel")
        assert result.is_valid
        assert result.score >= 60
        assert result.entropy_bits > 0

    def test_rejects_password_containing_email(self):
        assert not evaluate_password("MyEmail1234!x", email="myemail@corp.io").is_valid

    def test_rejects_password_containing_name(self):
        assert not evaluate_password("JohnSmith99!x", full_name="John Smith").is_valid

    def test_validate_raises_with_every_violation(self):
        with pytest.raises(PasswordPolicyError) as exc:
            validate_password("weak")
        assert len(exc.value.violations) >= 3

    def test_labels_span_the_range(self):
        assert evaluate_password("abc").label in {"very_weak", "weak"}
        assert evaluate_password("Tr0ub4dor&Kestrel").label in {"strong", "very_strong"}


# -------------------------------------------------------------------- JWT ---
class TestJwtClaims:
    def test_access_token_carries_registered_claims(self):
        claims = jwt.get_unverified_claims(create_access_token({"sub": "u1"}))
        for claim in ("jti", "iat", "nbf", "exp", "typ", "iss", "aud", "sub"):
            assert claim in claims, claim
        assert claims["typ"] == TOKEN_TYPE_ACCESS

    def test_refresh_token_is_typed_separately(self):
        claims = jwt.get_unverified_claims(create_refresh_token({"sub": "u1"}))
        assert claims["typ"] == TOKEN_TYPE_REFRESH

    def test_jti_is_unique_per_token(self):
        first = jwt.get_unverified_claims(create_access_token({"sub": "u1"}))["jti"]
        second = jwt.get_unverified_claims(create_access_token({"sub": "u1"}))["jti"]
        assert first != second

    def test_refresh_token_rejected_where_access_expected(self):
        token = create_refresh_token({"sub": "u1"})
        with pytest.raises(HTTPException):
            decode_token(token, expected_type=TOKEN_TYPE_ACCESS)

    def test_access_token_rejected_where_refresh_expected(self):
        token = create_access_token({"sub": "u1"})
        with pytest.raises(HTTPException):
            decode_token(token, expected_type=TOKEN_TYPE_REFRESH)

    def test_expired_token_rejected(self):
        token = create_access_token({"sub": "u1"}, expires_delta=timedelta(seconds=-3600))
        with pytest.raises(HTTPException):
            decode_token(token)

    def test_small_clock_skew_tolerated(self):
        token = create_access_token({"sub": "u1"}, expires_delta=timedelta(seconds=-5))
        assert decode_token(token)["sub"] == "u1"

    def test_tampered_signature_rejected(self):
        from app.core.config import settings

        claims = jwt.get_unverified_claims(create_access_token({"sub": "u1"}))
        forged = jwt.encode(
            claims, "a-different-secret-key-long-enough", algorithm=settings.ALGORITHM
        )
        with pytest.raises(HTTPException):
            decode_token(forged)

    def test_wrong_issuer_rejected(self):
        from app.core.config import settings

        claims = jwt.get_unverified_claims(create_access_token({"sub": "u1"}))
        claims["iss"] = "someone-else"
        token = jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        with pytest.raises(HTTPException):
            decode_token(token)

    def test_wrong_audience_rejected(self):
        from app.core.config import settings

        claims = jwt.get_unverified_claims(create_access_token({"sub": "u1"}))
        claims["aud"] = "someone-else"
        token = jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        with pytest.raises(HTTPException):
            decode_token(token)


class TestRoleRanking:
    def test_admin_outranks_member(self):
        assert role_rank("admin") > role_rank("member")

    def test_unknown_role_ranks_lowest(self):
        assert role_rank("nonsense") == 0
        assert role_rank("") == 0


# ------------------------------------------------------------------ audit ---
class TestAuditRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "hashed_password",
            "refresh_token",
            "api_key",
            "GEMINI_API_KEY",
            "Cookie",
            "Authorization",
            "client_secret",
        ],
    )
    def test_sensitive_keys_are_redacted(self, key):
        assert redact({key: "sensitive-value"})[key] == REDACTED

    def test_nested_secrets_are_redacted(self):
        assert redact({"outer": {"secret": "s3cr3t"}})["outer"]["secret"] == REDACTED

    def test_non_sensitive_values_survive(self):
        assert redact({"filename": "report.pdf"})["filename"] == "report.pdf"

    def test_jwt_shaped_value_redacted_by_shape(self):
        token = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
            ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
            ".dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        )
        assert redact({"innocuous": token})["innocuous"] == REDACTED

    def test_bearer_header_redacted_by_shape(self):
        assert redact({"innocuous": "Bearer abc123"})["innocuous"] == REDACTED

    def test_counts_under_sensitive_keys_are_preserved(self):
        # A count is not a secret; redacting it would strip useful signal.
        assert redact({"revoked_tokens": 3})["revoked_tokens"] == 3
        assert redact({"access_token_blacklisted": True})["access_token_blacklisted"] is True

    def test_long_strings_are_truncated(self):
        assert len(redact("x" * 5000)) < 2000

    def test_deep_nesting_is_bounded(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}
        assert "max-depth" in str(redact(deep))

    def test_action_and_outcome_are_string_valued(self):
        assert AuditAction.LOGIN_SUCCESS.value == "auth.login.success"
        assert AuditOutcome.DENIED.value == "denied"


# --------------------------------------------------------- configuration ---
class TestConfigurationValidation:
    @staticmethod
    def _prod(**overrides):
        base = {
            "ENVIRONMENT": "production",
            "SECRET_KEY": "a" * 48,
            "GEMINI_API_KEY": "key",
            "DATABASE_URL": "postgresql://u:p@db:5432/rs",
            "CORS_ORIGINS": "https://app.example.com",
            "FRONTEND_URL": "https://app.example.com",
            "TRUSTED_HOSTS": "app.example.com",
            "REDIS_URL": "redis://redis:6379",
            "DEBUG": False,
        }
        base.update(overrides)
        # _env_file=None keeps a developer's local .env out of the assertion.
        return Settings(_env_file=None, **base)

    def test_valid_production_config_passes(self):
        assert validate_configuration(self._prod())["errors"] == []

    def test_development_never_blocks_startup(self):
        report = validate_configuration(Settings(_env_file=None, ENVIRONMENT="development"))
        assert report["errors"] == []

    @pytest.mark.parametrize(
        "overrides,fragment",
        [
            ({"SECRET_KEY": "change-this-in-production-super-secret-key-32chars"}, "default"),
            ({"SECRET_KEY": "short"}, "too short"),
            ({"GEMINI_API_KEY": ""}, "GEMINI_API_KEY"),
            ({"DATABASE_URL": "sqlite:///./x.db"}, "SQLite"),
            ({"CORS_ORIGINS": "*", "FRONTEND_URL": "*"}, "Wildcard CORS"),
            ({"TRUSTED_HOSTS": "*"}, "TRUSTED_HOSTS"),
            ({"DEBUG": True}, "DEBUG"),
            ({"REDIS_URL": "http://nope"}, "REDIS_URL"),
            ({"QDRANT_HOST": ""}, "QDRANT_HOST"),
        ],
    )
    def test_production_rejects_bad_config(self, overrides, fragment):
        errors = " | ".join(validate_configuration(self._prod(**overrides))["errors"])
        assert fragment in errors, errors

    def test_csv_settings_parse_and_deduplicate(self):
        settings = Settings(
            _env_file=None,
            CORS_ORIGINS="http://a.com, http://b.com ,http://a.com",
            FRONTEND_URL="http://a.com",
        )
        assert settings.cors_origins == ["http://a.com", "http://b.com"]

    def test_blank_redis_url_disables_redis(self):
        assert not Settings(_env_file=None, REDIS_URL="").redis_enabled

    def test_blocklist_overrides_allowlist(self):
        settings = Settings(_env_file=None, ALLOWED_UPLOAD_EXTENSIONS="pdf,exe")
        assert "exe" not in settings.allowed_upload_extensions
        assert "pdf" in settings.allowed_upload_extensions
