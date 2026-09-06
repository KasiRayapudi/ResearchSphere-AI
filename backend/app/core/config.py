from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "ResearchSphere AI"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"  # development | staging | production

    # Security
    SECRET_KEY: str = "change-this-in-production-super-secret-key-32chars"
    ALGORITHM: str = "HS256"
    #: Default deliberately left at 7 days. The frontend has no refresh logic
    #: or 401 interceptor yet, so shortening this would silently log users out
    #: mid-session. Lower it once the client consumes /auth/refresh.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    JWT_ISSUER: str = "researchsphere-ai"
    JWT_AUDIENCE: str = "researchsphere-api"
    #: Clock skew tolerance when validating exp/nbf/iat.
    JWT_CLOCK_SKEW_SECONDS: int = 30

    # Password policy (see app/core/password_policy.py)
    PASSWORD_MIN_LENGTH: int = 12
    PASSWORD_MAX_LENGTH: int = 128
    PASSWORD_REQUIRE_UPPERCASE: bool = True
    PASSWORD_REQUIRE_LOWERCASE: bool = True
    PASSWORD_REQUIRE_DIGIT: bool = True
    PASSWORD_REQUIRE_SPECIAL: bool = True
    PASSWORD_BLOCK_COMMON: bool = True

    # Password reset (see app/core/password_reset.py)
    PASSWORD_RESET_TOKEN_TTL_MINUTES: int = 30
    #: Max active reset requests per account before further ones are ignored.
    PASSWORD_RESET_MAX_ACTIVE: int = 3

    # Database
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/researchsphere_db"

    # Qdrant
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "researchsphere_docs"

    # AI APIs
    GEMINI_API_KEY: str = ""

    # Embedding
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    EMBEDDING_DIMENSION: int = 384

    # File Storage
    UPLOAD_DIR: str = "./uploads"
    MAX_UPLOAD_SIZE_MB: int = 50
    #: Uploads land here first and are promoted only after validation+scanning.
    QUARANTINE_DIR: str = "./uploads/.quarantine"

    # Upload security
    #: Comma-separated allowlist. Defaults to the formats the extraction
    #: pipeline actually supports (see app/rag/document_processor.py) - adding
    #: an extension here without extraction support means accepting files that
    #: fail during processing.
    ALLOWED_UPLOAD_EXTENSIONS: str = "pdf,docx,txt,md,markdown,csv"
    #: Always rejected, even if someone adds them to the allowlist.
    BLOCKED_UPLOAD_EXTENSIONS: str = (
        "exe,dll,bat,cmd,com,sh,bash,ps1,js,mjs,py,rb,pl,jar,class,msi,scr,"
        "vbs,app,deb,rpm,so,dylib,zip,rar,7z,tar,gz,iso,img"
    )
    #: Reject a second upload of identical content (matched on SHA-256).
    ENABLE_DUPLICATE_DETECTION: bool = True
    #: Registered scanner name from app/services/antivirus.py.
    VIRUS_SCANNER: str = "noop"

    # CORS
    FRONTEND_URL: str = "http://localhost:3000"
    # Comma-separated list of additional allowed origins, e.g.
    # CORS_ORIGINS="http://localhost:3000,https://app.example.com"
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173"

    # Trusted hosts (comma-separated). "*" is only permitted outside production.
    TRUSTED_HOSTS: str = "localhost,127.0.0.1"

    # Redis (optional - Redis checks are skipped when this is blank)
    REDIS_URL: str = "redis://localhost:6379"

    # ------------------------------------------------------------------
    # Security headers (all configurable; see SecurityHeadersMiddleware)
    # ------------------------------------------------------------------
    SECURITY_HEADERS_ENABLED: bool = True
    HEADER_X_CONTENT_TYPE_OPTIONS: str = "nosniff"
    HEADER_X_FRAME_OPTIONS: str = "DENY"
    HEADER_REFERRER_POLICY: str = "strict-origin-when-cross-origin"
    HEADER_PERMISSIONS_POLICY: str = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    HEADER_CROSS_ORIGIN_RESOURCE_POLICY: str = "same-site"
    HEADER_CROSS_ORIGIN_OPENER_POLICY: str = "same-origin"
    #: Blank by default: require-corp breaks cross-origin resources that do not
    #: opt in, including the Swagger CDN. Enable deliberately.
    HEADER_CROSS_ORIGIN_EMBEDDER_POLICY: str = ""

    #: CSP for the API itself. The API returns JSON, so it needs almost nothing.
    CSP_DEFAULT: str = (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    )
    #: CSP for /docs and /redoc, which load assets from jsdelivr and inline
    #: their bootstrap script. A strict policy here renders a blank page.
    CSP_DOCS: str = (
        "default-src 'self'; "
        "img-src 'self' data: https://fastapi.tiangolo.com; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "font-src 'self' data: https://cdn.jsdelivr.net; "
        "worker-src 'self' blob:; "
        "frame-ancestors 'none'; base-uri 'none'"
    )

    # HSTS (production only - see SecurityHeadersMiddleware)
    HSTS_ENABLED: bool = True
    HSTS_MAX_AGE: int = 31536000  # 1 year
    HSTS_INCLUDE_SUBDOMAINS: bool = True
    HSTS_PRELOAD: bool = False

    model_config = {"env_file": ".env", "extra": "ignore"}

    @staticmethod
    def _split_csv(raw: str) -> list[str]:
        return [item.strip() for item in (raw or "").split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        """True only when ENVIRONMENT is explicitly 'production'.

        Deliberately not derived from DEBUG: DEBUG defaults to False, so
        deriving it would make every local run behave as production.
        """
        return self.ENVIRONMENT.strip().lower() == "production"

    @property
    def cors_origins(self) -> list[str]:
        """Allowed CORS origins, always including FRONTEND_URL, de-duplicated."""
        origins = self._split_csv(self.CORS_ORIGINS)
        if self.FRONTEND_URL and self.FRONTEND_URL not in origins:
            origins.append(self.FRONTEND_URL)
        # Preserve order while removing duplicates
        return list(dict.fromkeys(origins))

    @property
    def trusted_hosts(self) -> list[str]:
        hosts = self._split_csv(self.TRUSTED_HOSTS)
        return hosts or ["localhost", "127.0.0.1"]

    @property
    def redis_enabled(self) -> bool:
        return bool((self.REDIS_URL or "").strip())

    @property
    def allowed_upload_extensions(self) -> set:
        """Allowlist minus anything on the blocklist (blocklist always wins)."""
        allowed = {e.lower().lstrip(".") for e in self._split_csv(self.ALLOWED_UPLOAD_EXTENSIONS)}
        return allowed - self.blocked_upload_extensions

    @property
    def blocked_upload_extensions(self) -> set:
        return {e.lower().lstrip(".") for e in self._split_csv(self.BLOCKED_UPLOAD_EXTENSIONS)}

    @property
    def max_upload_bytes(self) -> int:
        return max(1, self.MAX_UPLOAD_SIZE_MB) * 1024 * 1024

    @property
    def hsts_value(self) -> str:
        parts = [f"max-age={self.HSTS_MAX_AGE}"]
        if self.HSTS_INCLUDE_SUBDOMAINS:
            parts.append("includeSubDomains")
        if self.HSTS_PRELOAD:
            parts.append("preload")
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# Startup configuration validation
# ---------------------------------------------------------------------------
#: Values that must never be used outside development.
INSECURE_SECRET_DEFAULTS = {
    "change-this-in-production-super-secret-key-32chars",
    "super-secret-jwt-key-researchsphere-2026-prod",
    "secret",
    "changeme",
}

MIN_SECRET_LENGTH = 32


class ConfigurationError(RuntimeError):
    """Raised when production configuration is missing or unsafe."""


def validate_configuration(config: "Settings" = None) -> dict:
    """Validate runtime configuration.

    Returns ``{"errors": [...], "warnings": [...]}``. In production the caller
    refuses to start when ``errors`` is non-empty; outside production the same
    findings are surfaced as warnings so local development stays frictionless.
    """
    cfg = config or settings
    errors: list[str] = []
    warnings: list[str] = []
    production = cfg.is_production

    def fail(message: str) -> None:
        (errors if production else warnings).append(message)

    # --- JWT / application secret ---
    secret = (cfg.SECRET_KEY or "").strip()
    if not secret:
        fail("SECRET_KEY is not set.")
    elif secret in INSECURE_SECRET_DEFAULTS:
        fail("SECRET_KEY is still set to a well-known default value.")
    elif len(secret) < MIN_SECRET_LENGTH:
        fail(
            f"SECRET_KEY is too short ({len(secret)} chars); "
            f"at least {MIN_SECRET_LENGTH} are required."
        )
    if cfg.ALGORITHM.upper().startswith("NONE"):
        errors.append("JWT ALGORITHM must not be 'none'.")

    # --- Gemini ---
    if not (cfg.GEMINI_API_KEY or "").strip():
        fail("GEMINI_API_KEY is not set; chat, research and reports will fail.")

    # --- Database ---
    db_url = (cfg.DATABASE_URL or "").strip()
    if not db_url:
        fail("DATABASE_URL is not set.")
    elif db_url.startswith("sqlite"):
        fail("DATABASE_URL points at SQLite, which is not supported in production.")
    elif production and ("@localhost" in db_url or "@127.0.0.1" in db_url):
        warnings.append("DATABASE_URL points at localhost in production.")

    # --- Redis (optional, but must be well-formed when configured) ---
    redis_url = (cfg.REDIS_URL or "").strip()
    if redis_url and not redis_url.startswith(("redis://", "rediss://", "unix://")):
        fail(f"REDIS_URL has an unsupported scheme: {redis_url.split(':', 1)[0]!r}")
    if production and not redis_url:
        warnings.append(
            "REDIS_URL is empty: rate limiting falls back to per-process "
            "in-memory counters and token revocation is disabled."
        )

    # --- Qdrant ---
    if not (cfg.QDRANT_HOST or "").strip():
        fail("QDRANT_HOST is not set.")
    if not (1 <= int(cfg.QDRANT_PORT) <= 65535):
        fail(f"QDRANT_PORT is out of range: {cfg.QDRANT_PORT}")
    if not (cfg.QDRANT_COLLECTION or "").strip():
        fail("QDRANT_COLLECTION is not set.")

    # --- CORS origins ---
    origins = cfg.cors_origins
    if not origins:
        fail("No CORS origins configured; the frontend will be blocked.")
    if "*" in origins:
        fail("Wildcard CORS origin '*' is not permitted in production.")
    for origin in origins:
        if origin != "*" and not origin.startswith(("http://", "https://")):
            fail(f"CORS origin is not a valid URL: {origin!r}")
        if production and origin.startswith("http://"):
            warnings.append(f"CORS origin uses plain HTTP in production: {origin}")

    # --- Trusted hosts ---
    hosts = cfg.trusted_hosts
    if not hosts:
        fail("No TRUSTED_HOSTS configured.")
    if production and "*" in hosts:
        errors.append("Wildcard TRUSTED_HOSTS '*' is not permitted in production.")

    # --- Upload paths ---
    for label, path_value in (
        ("UPLOAD_DIR", cfg.UPLOAD_DIR),
        ("QUARANTINE_DIR", cfg.QUARANTINE_DIR),
    ):
        if not (path_value or "").strip():
            fail(f"{label} is not set.")
            continue
        try:
            resolved = Path(path_value).resolve()
            resolved.mkdir(parents=True, exist_ok=True)
            probe = resolved / ".config_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except Exception as exc:
            fail(f"{label} ({path_value}) is not writable: {exc}")
    try:
        if Path(cfg.QUARANTINE_DIR).resolve() == Path(cfg.UPLOAD_DIR).resolve():
            fail("QUARANTINE_DIR must not be the same directory as UPLOAD_DIR.")
    except Exception:
        pass

    if cfg.MAX_UPLOAD_SIZE_MB <= 0:
        fail(f"MAX_UPLOAD_SIZE_MB must be positive (got {cfg.MAX_UPLOAD_SIZE_MB}).")
    if not cfg.allowed_upload_extensions:
        fail("ALLOWED_UPLOAD_EXTENSIONS is empty; every upload would be rejected.")

    # --- Misc production hygiene ---
    if production and cfg.DEBUG:
        errors.append("DEBUG must be disabled in production.")

    return {"errors": errors, "warnings": warnings}


settings = Settings()
