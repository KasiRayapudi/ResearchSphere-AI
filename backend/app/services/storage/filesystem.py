"""
Local filesystem storage.

The behaviour the product has always had, now behind the provider
interface. Suitable for a single-node deployment and for development; a
second replica cannot read what the first one wrote, which is exactly why
the S3 provider exists.

Keys are resolved under a configured root and the result is checked to be
inside it, so a key that somehow escaped validation still cannot reach
outside the storage directory.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import get_logger

from .base import ObjectInfo, ObjectNotFound, StorageError, StorageProvider, validate_key

logger = get_logger("storage.filesystem")


class FilesystemStorage(StorageProvider):
    name = "filesystem"

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """Absolute path for a key, guaranteed to be under the root.

        The key is validated first, but this check is what actually holds:
        resolve() collapses any traversal, and a result outside the root is
        refused rather than trusted.
        """
        validate_key(key)
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise StorageError(f"Storage key escapes the storage root: {key!r}")
        return candidate

    def put(self, key: str, source_path: str, *, content_type: str | None = None) -> ObjectInfo:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # os.replace is atomic within a filesystem; shutil.move handles
            # the case where quarantine and storage are on different mounts.
            try:
                os.replace(source_path, destination)
            except OSError:
                shutil.move(source_path, destination)
        except OSError as exc:
            raise StorageError(f"Could not store {key}: {exc}") from exc

        stat = destination.stat()
        return ObjectInfo(
            key=key,
            size=stat.st_size,
            content_type=content_type,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    def download_to(self, key: str, destination_path: str) -> str:
        source = self._path(key)
        if not source.is_file():
            raise ObjectNotFound(f"No stored object for key {key!r}")
        Path(destination_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination_path)
        return destination_path

    def local_path(self, key: str) -> str:
        """The real path of an object.

        Filesystem-only, used by the download endpoint to stream without
        copying. Not part of the interface: no other provider can offer it,
        and callers must fall back to download_to.
        """
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFound(f"No stored object for key {key!r}")
        return str(path)

    def delete(self, key: str) -> bool:
        try:
            path = self._path(key)
        except StorageError:
            # A malformed key cannot refer to anything we stored, so there
            # is nothing to delete and nothing to fail about.
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            # On Windows a handle held by a failed extractor can block this.
            # Cleanup runs again later; a delete that could not happen must
            # not take the caller down with it.
            logger.warning(f"Could not delete {key}: {exc}")
            return False

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).is_file()
        except StorageError:
            return False

    def info(self, key: str) -> ObjectInfo:
        path = self._path(key)
        if not path.is_file():
            raise ObjectNotFound(f"No stored object for key {key!r}")
        stat = path.stat()
        return ObjectInfo(
            key=key,
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    def list(self, prefix: str = "", limit: int = 1000) -> list[ObjectInfo]:
        base = self.root
        if prefix:
            candidate = (self.root / prefix).resolve()
            if self.root not in candidate.parents and candidate != self.root:
                raise StorageError(f"Listing prefix escapes the storage root: {prefix!r}")
            base = candidate
        if not base.exists():
            return []

        found: list[ObjectInfo] = []
        for path in sorted(base.rglob("*")):
            if len(found) >= limit:
                break
            if not path.is_file():
                continue
            stat = path.stat()
            found.append(
                ObjectInfo(
                    key=path.relative_to(self.root).as_posix(),
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
        return found

    def signed_url(self, key: str, *, expires_in: int, filename: str | None = None) -> str | None:
        """None: there is no URL to sign.

        The download endpoint streams the file itself rather than handing
        out a path, which is the only correct answer here -- exposing a
        local path would tell a caller about the host's directory layout.
        """
        return None

    def health(self) -> dict:
        probe = self.root / ".storage_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return {"provider": self.name, "status": "ok", "root": str(self.root)}
        except OSError as exc:
            return {"provider": self.name, "status": "failed", "error": str(exc)}
