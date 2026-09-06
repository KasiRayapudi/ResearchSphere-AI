from pydantic_settings import BaseSettings
from typing import List, Optional
import os


class Settings(BaseSettings):
    # Application
    APP_NAME: str = "ResearchSphere AI"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"  # development | staging | production

    # Security
    SECRET_KEY: str = "change-this-in-production-super-secret-key-32chars"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080  # 7 days

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

    model_config = {"env_file": ".env", "extra": "ignore"}

    @staticmethod
    def _split_csv(raw: str) -> List[str]:
        return [item.strip() for item in (raw or "").split(",") if item.strip()]

    @property
    def is_production(self) -> bool:
        """True only when ENVIRONMENT is explicitly 'production'.

        Deliberately not derived from DEBUG: DEBUG defaults to False, so
        deriving it would make every local run behave as production.
        """
        return self.ENVIRONMENT.strip().lower() == "production"

    @property
    def cors_origins(self) -> List[str]:
        """Allowed CORS origins, always including FRONTEND_URL, de-duplicated."""
        origins = self._split_csv(self.CORS_ORIGINS)
        if self.FRONTEND_URL and self.FRONTEND_URL not in origins:
            origins.append(self.FRONTEND_URL)
        # Preserve order while removing duplicates
        return list(dict.fromkeys(origins))

    @property
    def trusted_hosts(self) -> List[str]:
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


settings = Settings()
