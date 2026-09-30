"""
Storage provider selection.

Which backend is active is a configuration decision, made once, here.
Nothing in the API or worker imports a concrete provider.
"""

from __future__ import annotations

import re
import uuid

from app.core.config import settings
from app.core.logging import get_logger

from .base import ObjectInfo, ObjectNotFound, StorageError, StorageProvider, validate_key
from .filesystem import FilesystemStorage
from .s3 import AzureBlobStorage, S3Storage

logger = get_logger("storage")

__all__ = [
    "AzureBlobStorage",
    "FilesystemStorage",
    "ObjectInfo",
    "ObjectNotFound",
    "S3Storage",
    "StorageError",
    "StorageProvider",
    "build_document_key",
    "get_storage",
    "legacy_key",
    "resolve_key",
    "reset_storage",
    "validate_key",
]

#: Prefix under which document objects live, so the cleanup task can list
#: them without walking unrelated keys.
DOCUMENT_PREFIX = "documents"
#: Prefix for anything written while an upload is still in flight.
TEMP_PREFIX = "tmp"

_provider: StorageProvider | None = None


def build_document_key(workspace_id: str, extension: str) -> str:
    """An opaque key for a new document.

    The client's filename never contributes to it. Partitioning by workspace
    keeps a listing scoped to one tenant, which is what makes the orphan
    sweep affordable, and the random name means two uploads of the same file
    cannot collide or overwrite each other.
    """
    safe_extension = re.sub(r"[^a-z0-9]", "", (extension or "").lower())[:10]
    safe_workspace = re.sub(r"[^A-Za-z0-9_-]", "", workspace_id or "")[:64] or "unassigned"
    name = uuid.uuid4().hex
    suffix = f".{safe_extension}" if safe_extension else ""
    return f"{DOCUMENT_PREFIX}/{safe_workspace}/{name}{suffix}"


def legacy_key(file_path: str) -> str:
    """The storage key for a document written before Phase 6.

    Those uploads were promoted straight into UPLOAD_DIR under an opaque
    name, so the basename of the recorded path is already the key relative to
    the filesystem root -- no rewriting, no re-upload, and no dependence on
    the absolute path still being correct on this host.
    """
    return (file_path or "").replace("\\", "/").rsplit("/", 1)[-1]


def resolve_key(storage_key: str | None, file_path: str | None) -> str:
    """The key for a document row, whichever era it was written in.

    Returns "" when neither is usable, which the caller treats as a missing
    object rather than guessing at a location.
    """
    if storage_key:
        return storage_key
    return legacy_key(file_path or "")


def build_provider() -> StorageProvider:
    """Construct the configured provider.

    Raises rather than falling back: a deployment that asked for S3 and
    silently got local disk would look healthy while writing documents a
    second replica cannot read.
    """
    choice = (settings.STORAGE_PROVIDER or "filesystem").strip().lower()

    if choice == "filesystem":
        return FilesystemStorage(root=settings.FILESYSTEM_ROOT or settings.UPLOAD_DIR)

    if choice in {"s3", "minio"}:
        if choice == "minio":
            return S3Storage(
                bucket=settings.MINIO_BUCKET or settings.AWS_BUCKET,
                endpoint_url=settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY,
                secret_key=settings.MINIO_SECRET_KEY,
                region=settings.AWS_REGION,
                # MinIO addresses buckets by path, not by subdomain.
                use_path_style=True,
                provider_name="minio",
            )
        return S3Storage(
            bucket=settings.AWS_BUCKET,
            region=settings.AWS_REGION,
            endpoint_url=settings.AWS_ENDPOINT_URL or None,
            access_key=settings.AWS_ACCESS_KEY_ID,
            secret_key=settings.AWS_SECRET_ACCESS_KEY,
            use_path_style=bool(settings.AWS_ENDPOINT_URL),
            provider_name="s3",
        )

    if choice == "azure":
        return AzureBlobStorage()

    raise StorageError(
        f"Unknown STORAGE_PROVIDER {choice!r}. Choose one of: filesystem, s3, minio."
    )


def get_storage() -> StorageProvider:
    """The active provider, constructed once per process."""
    global _provider
    if _provider is None:
        _provider = build_provider()
        logger.info(f"Storage provider: {_provider.name}")
    return _provider


def reset_storage() -> None:
    """Drop the cached provider so configuration changes take effect."""
    global _provider
    _provider = None
