"""
S3-compatible object storage.

One implementation covers both AWS S3 and MinIO: MinIO speaks the S3 API,
so the only difference is an endpoint URL and how addressing is configured.
Keeping them as one class means the code path a developer exercises against
MinIO is the code path that runs in production against S3.

Credentials are never logged and never leave this module. What the rest of
the application sees is a key.
"""

from __future__ import annotations

import os

from app.core.logging import get_logger

from .base import ObjectInfo, ObjectNotFound, StorageError, StorageProvider, validate_key

logger = get_logger("storage.s3")


class S3Storage(StorageProvider):
    """Objects in an S3 bucket.

    ``endpoint_url`` set means MinIO or another S3-compatible service;
    absent means AWS.
    """

    def __init__(
        self,
        *,
        bucket: str,
        region: str | None = None,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        use_path_style: bool = False,
        provider_name: str = "s3",
    ):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - import guard
            raise StorageError(
                "boto3 is required for S3 storage. Install it, or set "
                "STORAGE_PROVIDER=filesystem."
            ) from exc

        self.name = provider_name
        self.bucket = bucket

        config = Config(
            # MinIO and most S3-compatible services need path-style
            # addressing; AWS prefers virtual-host style.
            s3={"addressing_style": "path" if use_path_style else "auto"},
            # Must be explicit. Left to the default, botocore presigns with
            # SigV2 (AWSAccessKeyId/Signature/Expires), which every region
            # created after 2014 rejects outright and which cannot sign
            # SSE-KMS objects -- so downloads would fail in production while
            # working locally.
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
        )
        # Credentials are passed explicitly when configured, otherwise boto3
        # resolves them from the environment, an instance profile or an
        # IRSA role -- which is how this should be deployed on AWS.
        self._client = boto3.client(
            "s3",
            region_name=region or None,
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            config=config,
        )

    # ------------------------------------------------------------ helpers --
    def _is_missing(self, exc: Exception) -> bool:
        """Whether a botocore error means "no such object"."""
        response = getattr(exc, "response", None) or {}
        code = str(response.get("Error", {}).get("Code", ""))
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return code in {"404", "NoSuchKey", "NotFound"} or status == 404

    # ---------------------------------------------------------- interface --
    def put(self, key: str, source_path: str, *, content_type: str | None = None) -> ObjectInfo:
        validate_key(key)
        extra = {"ContentType": content_type} if content_type else {}
        try:
            # upload_file streams in parts, so a large document never has to
            # be held in memory.
            self._client.upload_file(source_path, self.bucket, key, ExtraArgs=extra or None)
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise StorageError(f"Could not store {key}: {exc}") from exc

        # upload_file copies; the interface says put() consumes its source.
        # Only after the object is confirmed stored, so a failed upload
        # leaves the quarantined file for the caller to handle.
        try:
            os.unlink(source_path)
        except OSError as exc:
            # The object is safely stored, so this must not fail the upload.
            # The temporary-file sweep collects what is left behind.
            logger.warning(f"Could not remove source file after storing {key}: {exc}")

        return ObjectInfo(
            key=key,
            size=int(head.get("ContentLength", 0)),
            content_type=head.get("ContentType"),
            modified_at=head.get("LastModified"),
            metadata={"etag": str(head.get("ETag", "")).strip('"')},
        )

    def download_to(self, key: str, destination_path: str) -> str:
        validate_key(key)
        try:
            self._client.download_file(self.bucket, key, destination_path)
        except Exception as exc:
            if self._is_missing(exc):
                raise ObjectNotFound(f"No stored object for key {key!r}") from exc
            raise StorageError(f"Could not download {key}: {exc}") from exc
        return destination_path

    def delete(self, key: str) -> bool:
        try:
            validate_key(key)
        except StorageError:
            return False
        try:
            # S3 delete is idempotent and reports success for a key that was
            # never there, so existence is checked first to give the caller
            # an honest answer.
            existed = self.exists(key)
            self._client.delete_object(Bucket=self.bucket, Key=key)
            return existed
        except Exception as exc:
            logger.warning(f"Could not delete {key}: {exc}")
            return False

    def exists(self, key: str) -> bool:
        try:
            validate_key(key)
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except StorageError:
            return False
        except Exception as exc:
            if self._is_missing(exc):
                return False
            raise StorageError(f"Could not check {key}: {exc}") from exc

    def info(self, key: str) -> ObjectInfo:
        validate_key(key)
        try:
            head = self._client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._is_missing(exc):
                raise ObjectNotFound(f"No stored object for key {key!r}") from exc
            raise StorageError(f"Could not read {key}: {exc}") from exc
        return ObjectInfo(
            key=key,
            size=int(head.get("ContentLength", 0)),
            content_type=head.get("ContentType"),
            modified_at=head.get("LastModified"),
            metadata={"etag": str(head.get("ETag", "")).strip('"')},
        )

    def list(self, prefix: str = "", limit: int = 1000) -> list[ObjectInfo]:
        found: list[ObjectInfo] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(
                Bucket=self.bucket,
                Prefix=prefix,
                PaginationConfig={"MaxItems": limit, "PageSize": min(limit, 1000)},
            ):
                for item in page.get("Contents", []):
                    found.append(
                        ObjectInfo(
                            key=item["Key"],
                            size=int(item.get("Size", 0)),
                            modified_at=item.get("LastModified"),
                            metadata={"etag": str(item.get("ETag", "")).strip('"')},
                        )
                    )
                    if len(found) >= limit:
                        return found
        except Exception as exc:
            raise StorageError(f"Could not list {prefix!r}: {exc}") from exc
        return found

    def signed_url(self, key: str, *, expires_in: int, filename: str | None = None) -> str | None:
        """A pre-signed GET URL.

        The signature is what authorises the fetch, and it expires, so the
        bucket itself stays private. The download filename is set through
        Content-Disposition so the browser saves the document under its
        original name rather than the opaque key.
        """
        validate_key(key)
        params: dict = {"Bucket": self.bucket, "Key": key}
        if filename:
            safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        try:
            return self._client.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=expires_in
            )
        except Exception as exc:
            raise StorageError(f"Could not sign a URL for {key}: {exc}") from exc

    def health(self) -> dict:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            # The bucket name is infrastructure detail; it is reported here
            # because this is an operator-facing health check, not a user
            # response.
            return {"provider": self.name, "status": "ok", "bucket": self.bucket}
        except Exception as exc:
            return {"provider": self.name, "status": "failed", "error": str(exc)[:200]}


class AzureBlobStorage(StorageProvider):
    """Placeholder for Azure Blob Storage.

    Deliberately not implemented. It is declared so the provider registry
    has a name to reject clearly, rather than a deployment discovering the
    gap through a confusing configuration error -- and so nobody mistakes an
    empty class for working support.
    """

    name = "azure"

    def __init__(self, *_args, **_kwargs):
        raise StorageError(
            "Azure Blob Storage is not implemented. Use STORAGE_PROVIDER=s3 "
            "(which also serves MinIO) or filesystem."
        )

    def put(self, key, source_path, *, content_type=None):  # pragma: no cover
        raise NotImplementedError

    def download_to(self, key, destination_path):  # pragma: no cover
        raise NotImplementedError

    def delete(self, key):  # pragma: no cover
        raise NotImplementedError

    def exists(self, key):  # pragma: no cover
        raise NotImplementedError

    def info(self, key):  # pragma: no cover
        raise NotImplementedError

    def list(self, prefix="", limit=1000):  # pragma: no cover
        raise NotImplementedError

    def signed_url(self, key, *, expires_in, filename=None):  # pragma: no cover
        raise NotImplementedError
