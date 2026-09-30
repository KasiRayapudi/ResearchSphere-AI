"""
Object storage: providers, the pipeline that uses them, and housekeeping.

The S3 tests run against moto, which serves the real botocore request path in
process. That matters more than convenience: a hand-written stub of the boto3
client would agree with whatever this code does, including its mistakes, and
would not have caught the presigning defect these tests exist to prevent
regressing.
"""

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.config import settings
from app.services.storage import (
    DOCUMENT_PREFIX,
    FilesystemStorage,
    ObjectNotFound,
    StorageError,
    build_document_key,
    get_storage,
    legacy_key,
    reset_storage,
    resolve_key,
    validate_key,
)

BUCKET = "researchsphere-test"


# ---------------------------------------------------------------- fixtures --
@pytest.fixture
def local_storage(tmp_path):
    """A filesystem provider on a throwaway root."""
    return FilesystemStorage(root=str(tmp_path / "objects"))


@pytest.fixture
def sample_file(tmp_path):
    def _make(content: bytes = b"storage test payload", name: str = "sample.txt") -> str:
        path = tmp_path / f"{uuid.uuid4().hex}-{name}"
        path.write_bytes(content)
        return str(path)

    return _make


@pytest.fixture
def s3_storage():
    """A real S3 client talking to moto, plus the bucket it expects."""
    from moto import mock_aws

    with mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        from app.services.storage.s3 import S3Storage

        yield S3Storage(
            bucket=BUCKET, region="us-east-1", access_key="testing", secret_key="testing"
        )


@pytest.fixture
def provider_settings():
    """Swap the configured provider for one test and put it back afterwards."""
    saved = {
        name: getattr(settings, name)
        for name in (
            "STORAGE_PROVIDER",
            "FILESYSTEM_ROOT",
            "AWS_BUCKET",
            "AWS_REGION",
            "AWS_ENDPOINT_URL",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "MINIO_BUCKET",
            "MINIO_ENDPOINT",
            "MINIO_ACCESS_KEY",
            "MINIO_SECRET_KEY",
        )
    }

    def _apply(**values):
        for name, value in values.items():
            setattr(settings, name, value)
        reset_storage()
        return get_storage()

    yield _apply

    for name, value in saved.items():
        setattr(settings, name, value)
    reset_storage()


@pytest.fixture
def s3_backed_app(provider_settings):
    """Point the running application at an in-process S3 for one test."""
    from moto import mock_aws

    with mock_aws():
        import boto3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield provider_settings(
            STORAGE_PROVIDER="s3",
            AWS_BUCKET=BUCKET,
            AWS_REGION="us-east-1",
            AWS_ENDPOINT_URL="",
            AWS_ACCESS_KEY_ID="testing",
            AWS_SECRET_ACCESS_KEY="testing",
        )


def _upload(client, headers, workspace_id, content=b"Storage layer document. " * 30):
    response = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": (f"{uuid.uuid4().hex}.txt", content, "text/plain")},
        data={"workspace_id": workspace_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _register_outsider(client):
    """A second account, unrelated to the fixtures' user.

    Not the admin_user fixture: that shares the function-scoped unique_email
    with auth_headers, so requesting both in one test makes the second signup
    collide on an address that already exists.
    """
    email = f"outsider_{uuid.uuid4().hex[:10]}@example.com"
    password = "Xk9#mQp2$vLw7"
    response = client.post(
        "/api/v1/auth/signup",
        json={"name": "Outsider", "email": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _error_message(response) -> str:
    """The human-readable part of the application's error envelope."""
    return response.json()["error"]["message"]


def _document(document_id):
    from app.core.database import SessionLocal
    from app.models.document import Document

    db = SessionLocal()
    try:
        return db.query(Document).filter(Document.id == document_id).first()
    finally:
        db.close()


# ------------------------------------------------------------------- keys --
class TestKeyValidation:
    @pytest.mark.parametrize(
        "key",
        [
            "documents/ws-1/abc123.pdf",
            "documents/ws-1/abc123",
            "a",
            "tmp/x.y-z_1.txt",
        ],
    )
    def test_accepts_well_formed_keys(self, key):
        assert validate_key(key) == key

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "../etc/passwd",
            "documents/../../etc/passwd",
            "/absolute/path",
            "C:\\Windows\\system32",
            ".hidden",
            "documents/ws/..",
            "documents/ws/file name.pdf",
            "documents/ws/file\x00.pdf",
            "documents/ws/file\n.pdf",
            "a" * 513,
        ],
    )
    def test_rejects_malformed_keys(self, key):
        with pytest.raises(StorageError):
            validate_key(key)

    def test_document_key_never_derives_from_the_client_filename(self):
        key = build_document_key("ws-1", "pdf")
        assert "../" not in key
        assert key.startswith(f"{DOCUMENT_PREFIX}/ws-1/")
        assert key.endswith(".pdf")
        validate_key(key)

    def test_document_keys_are_unique(self):
        keys = {build_document_key("ws-1", "pdf") for _ in range(200)}
        assert len(keys) == 200

    @pytest.mark.parametrize(
        "workspace,extension",
        [
            ("../../etc", "pdf"),
            ("ws/../../other", "p df"),
            ("", ""),
            ("ws-1", "../sh"),
            ("ws-1", "PDF"),
        ],
    )
    def test_hostile_inputs_still_produce_a_valid_key(self, workspace, extension):
        validate_key(build_document_key(workspace, extension))

    def test_legacy_paths_reduce_to_a_key(self):
        assert legacy_key(r"C:\srv\uploads\ab12.pdf") == "ab12.pdf"
        assert legacy_key("/var/lib/rs/uploads/ab12.pdf") == "ab12.pdf"
        assert resolve_key("documents/w/x.pdf", "/old/y.pdf") == "documents/w/x.pdf"
        assert resolve_key(None, "/old/y.pdf") == "y.pdf"
        assert resolve_key(None, None) == ""


# ----------------------------------------------------- filesystem provider --
class TestFilesystemProvider:
    def test_round_trip(self, local_storage, sample_file, tmp_path):
        key = build_document_key("ws-1", "txt")
        info = local_storage.put(key, sample_file(b"hello"), content_type="text/plain")
        assert info.key == key
        assert info.size == 5
        assert local_storage.exists(key)
        assert local_storage.info(key).size == 5

        destination = str(tmp_path / "out.txt")
        local_storage.download_to(key, destination)
        assert Path(destination).read_bytes() == b"hello"
        assert Path(local_storage.local_path(key)).read_bytes() == b"hello"

    def test_put_consumes_the_source(self, local_storage, sample_file):
        source = sample_file()
        local_storage.put(build_document_key("ws-1", "txt"), source)
        assert not Path(source).exists(), "quarantine copy should not be left behind"

    def test_delete_is_idempotent(self, local_storage, sample_file):
        key = build_document_key("ws-1", "txt")
        local_storage.put(key, sample_file())
        assert local_storage.delete(key) is True
        assert local_storage.delete(key) is False
        assert not local_storage.exists(key)

    def test_missing_object_raises(self, local_storage, tmp_path):
        with pytest.raises(ObjectNotFound):
            local_storage.info("documents/ws-1/missing.txt")
        with pytest.raises(ObjectNotFound):
            local_storage.download_to("documents/ws-1/missing.txt", str(tmp_path / "x"))
        with pytest.raises(ObjectNotFound):
            local_storage.local_path("documents/ws-1/missing.txt")

    @pytest.mark.parametrize(
        "key", ["../escape.txt", "documents/../../escape.txt", "/etc/passwd", ".."]
    )
    def test_traversal_cannot_reach_outside_the_root(self, local_storage, key, sample_file):
        with pytest.raises(StorageError):
            local_storage.put(key, sample_file())
        assert local_storage.exists(key) is False
        assert local_storage.delete(key) is False

    def test_listing_is_scoped_and_bounded(self, local_storage, sample_file):
        for _ in range(5):
            local_storage.put(build_document_key("ws-1", "txt"), sample_file())
        local_storage.put(build_document_key("ws-2", "txt"), sample_file())

        assert len(local_storage.list(f"{DOCUMENT_PREFIX}/ws-1")) == 5
        assert len(local_storage.list(f"{DOCUMENT_PREFIX}/ws-2")) == 1
        assert len(local_storage.list(DOCUMENT_PREFIX)) == 6
        assert len(local_storage.list(DOCUMENT_PREFIX, limit=2)) == 2
        assert local_storage.list("documents/nonexistent") == []

    def test_listing_prefix_cannot_escape_the_root(self, local_storage):
        with pytest.raises(StorageError):
            local_storage.list("../..")

    def test_signs_no_url(self, local_storage, sample_file):
        key = build_document_key("ws-1", "txt")
        local_storage.put(key, sample_file())
        assert local_storage.signed_url(key, expires_in=300) is None

    def test_health_probes_the_root(self, local_storage):
        assert local_storage.health()["status"] == "ok"


# ------------------------------------------------------------ s3 provider --
class TestS3Provider:
    def test_round_trip(self, s3_storage, sample_file, tmp_path):
        key = build_document_key("ws-1", "pdf")
        info = s3_storage.put(key, sample_file(b"x" * 2048), content_type="application/pdf")
        assert info.size == 2048
        assert info.content_type == "application/pdf"
        assert info.metadata.get("etag")

        assert s3_storage.exists(key) is True
        assert s3_storage.info(key).size == 2048

        destination = str(tmp_path / "out.pdf")
        s3_storage.download_to(key, destination)
        assert Path(destination).read_bytes() == b"x" * 2048

    def test_missing_object_raises_not_found(self, s3_storage, tmp_path):
        assert s3_storage.exists("documents/ws-1/missing.pdf") is False
        with pytest.raises(ObjectNotFound):
            s3_storage.info("documents/ws-1/missing.pdf")
        with pytest.raises(ObjectNotFound):
            s3_storage.download_to("documents/ws-1/missing.pdf", str(tmp_path / "x"))

    def test_delete_reports_whether_something_went(self, s3_storage, sample_file):
        key = build_document_key("ws-1", "pdf")
        s3_storage.put(key, sample_file())
        assert s3_storage.delete(key) is True
        assert s3_storage.delete(key) is False

    def test_malformed_keys_are_refused(self, s3_storage, sample_file, tmp_path):
        with pytest.raises(StorageError):
            s3_storage.put("../evil", sample_file())
        with pytest.raises(StorageError):
            s3_storage.download_to("../evil", str(tmp_path / "x"))
        assert s3_storage.exists("../evil") is False
        assert s3_storage.delete("../evil") is False

    def test_listing_is_scoped_and_bounded(self, s3_storage, sample_file):
        for _ in range(4):
            s3_storage.put(build_document_key("ws-1", "pdf"), sample_file())
        s3_storage.put(build_document_key("ws-2", "pdf"), sample_file())

        assert len(s3_storage.list(f"{DOCUMENT_PREFIX}/ws-1")) == 4
        assert len(s3_storage.list(DOCUMENT_PREFIX)) == 5
        assert len(s3_storage.list(DOCUMENT_PREFIX, limit=2)) == 2

    def test_signed_url_uses_sigv4(self, s3_storage, sample_file):
        """Regression guard.

        Left to botocore's default the presigner emits SigV2
        (AWSAccessKeyId/Signature/Expires), which every AWS region created
        after 2014 rejects outright -- downloads would have worked in
        development and failed in production.
        """
        key = build_document_key("ws-1", "pdf")
        s3_storage.put(key, sample_file())
        url = s3_storage.signed_url(key, expires_in=300)

        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
        assert "X-Amz-Signature=" in url
        assert "X-Amz-Expires=300" in url
        assert "AWSAccessKeyId=" not in url

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("Quarterly Report.pdf", 'attachment; filename="Quarterly Report.pdf"'),
            # An embedded quote would close the header value early; a newline
            # would let the caller append headers of their own.
            ('Quarterly "Report".pdf', 'attachment; filename="Quarterly Report.pdf"'),
            (
                "evil\r\nX-Injected: 1.pdf",
                'attachment; filename="evilX-Injected: 1.pdf"',
            ),
        ],
    )
    def test_signed_url_carries_a_sanitised_filename(
        self, s3_storage, sample_file, filename, expected
    ):
        from urllib.parse import parse_qs, urlparse

        key = build_document_key("ws-1", "pdf")
        s3_storage.put(key, sample_file())
        url = s3_storage.signed_url(key, expires_in=60, filename=filename)

        disposition = parse_qs(urlparse(url).query)["response-content-disposition"][0]
        assert disposition == expected
        assert disposition.count('"') == 2
        assert chr(13) not in disposition and chr(10) not in disposition

    def test_signed_url_expiry_is_configurable(self, s3_storage, sample_file):
        key = build_document_key("ws-1", "pdf")
        s3_storage.put(key, sample_file())
        assert "X-Amz-Expires=60" in s3_storage.signed_url(key, expires_in=60)
        assert "X-Amz-Expires=3600" in s3_storage.signed_url(key, expires_in=3600)

    def test_signed_url_refuses_a_malformed_key(self, s3_storage):
        with pytest.raises(StorageError):
            s3_storage.signed_url("../evil", expires_in=60)

    def test_health_reports_the_bucket(self, s3_storage):
        health = s3_storage.health()
        assert health["status"] == "ok"
        assert health["provider"] == "s3"

    def test_health_reports_failure_for_a_missing_bucket(self):
        from moto import mock_aws

        with mock_aws():
            from app.services.storage.s3 import S3Storage

            storage = S3Storage(
                bucket="does-not-exist",
                region="us-east-1",
                access_key="testing",
                secret_key="testing",
            )
            assert storage.health()["status"] == "failed"


# ------------------------------------------------------ provider selection --
class TestProviderSelection:
    def test_filesystem_is_selected_and_rooted(self, provider_settings, tmp_path):
        root = tmp_path / "fsroot"
        storage = provider_settings(STORAGE_PROVIDER="filesystem", FILESYSTEM_ROOT=str(root))
        assert storage.name == "filesystem"
        assert storage.root == root.resolve()

    def test_filesystem_falls_back_to_upload_dir(self, provider_settings):
        storage = provider_settings(STORAGE_PROVIDER="filesystem", FILESYSTEM_ROOT="")
        assert storage.root == Path(settings.UPLOAD_DIR).resolve()

    def test_s3_is_selected(self, provider_settings):
        from moto import mock_aws

        with mock_aws():
            storage = provider_settings(
                STORAGE_PROVIDER="s3",
                AWS_BUCKET=BUCKET,
                AWS_REGION="us-east-1",
                AWS_ACCESS_KEY_ID="testing",
                AWS_SECRET_ACCESS_KEY="testing",
            )
        assert storage.name == "s3"
        assert storage.bucket == BUCKET

    def test_minio_uses_path_style_addressing(self, provider_settings):
        from moto import mock_aws

        with mock_aws():
            storage = provider_settings(
                STORAGE_PROVIDER="minio",
                MINIO_BUCKET="minio-bucket",
                MINIO_ENDPOINT="http://minio.internal:9000",
                MINIO_ACCESS_KEY="minio",
                MINIO_SECRET_KEY="minio-secret",
            )
        assert storage.name == "minio"
        assert storage._client.meta.config.s3["addressing_style"] == "path"

    def test_azure_is_refused_rather_than_pretending(self, provider_settings):
        with pytest.raises(StorageError, match="not implemented"):
            provider_settings(STORAGE_PROVIDER="azure")

    def test_unknown_provider_is_refused(self, provider_settings):
        with pytest.raises(StorageError, match="not recognised|Unknown"):
            provider_settings(STORAGE_PROVIDER="dropbox")

    def test_provider_is_built_once(self, provider_settings):
        provider_settings(STORAGE_PROVIDER="filesystem")
        assert get_storage() is get_storage()
        reset_storage()
        assert get_storage() is not None

    def test_business_logic_never_imports_a_concrete_provider(self):
        """The abstraction is only real if nothing reaches past it."""
        offenders = []
        for module in ("app/api/v1/documents.py", "app/worker/tasks.py"):
            source = Path(module).read_text(encoding="utf-8")
            for marker in ("FilesystemStorage", "S3Storage", "boto3", "AzureBlobStorage"):
                if marker in source:
                    offenders.append(f"{module}: {marker}")
        assert offenders == []


# --------------------------------------------------- configuration guards --
class TestStorageConfiguration:
    """Production must refuse to start on storage configuration it cannot use."""

    @staticmethod
    def _production(**overrides):
        from app.core.config import Settings, validate_configuration

        base = {
            "ENVIRONMENT": "production",
            "SECRET_KEY": "a" * 48,
            "DATABASE_URL": "postgresql://u:p@db/rs",
            "GEMINI_API_KEY": "x",
            "QDRANT_HOST": "qdrant",
            "CORS_ORIGINS": "https://app.example.com",
            "TRUSTED_HOSTS": "app.example.com",
            "DEBUG": False,
        }
        base.update(overrides)
        return validate_configuration(Settings(**base))

    def test_s3_without_a_bucket_is_an_error(self):
        errors = self._production(STORAGE_PROVIDER="s3", AWS_BUCKET="")["errors"]
        assert any("AWS_BUCKET" in e for e in errors)

    def test_s3_with_half_a_credential_pair_is_an_error(self):
        errors = self._production(
            STORAGE_PROVIDER="s3", AWS_BUCKET="b", AWS_ACCESS_KEY_ID="only-this"
        )["errors"]
        assert any("AWS_SECRET_ACCESS_KEY" in e for e in errors)

    def test_s3_with_an_instance_role_is_accepted(self):
        result = self._production(STORAGE_PROVIDER="s3", AWS_BUCKET="b", AWS_REGION="eu-west-1")
        assert not [e for e in result["errors"] if "AWS" in e]

    @pytest.mark.parametrize(
        "overrides,expected",
        [
            ({"MINIO_ENDPOINT": ""}, "MINIO_ENDPOINT"),
            ({"MINIO_ENDPOINT": "minio:9000"}, "scheme"),
            ({"MINIO_BUCKET": "", "AWS_BUCKET": ""}, "MINIO_BUCKET"),
            ({"MINIO_SECRET_KEY": ""}, "MINIO_ACCESS_KEY"),
        ],
    )
    def test_minio_configuration_is_checked(self, overrides, expected):
        base = {
            "STORAGE_PROVIDER": "minio",
            "MINIO_ENDPOINT": "https://minio.internal:9000",
            "MINIO_BUCKET": "b",
            "MINIO_ACCESS_KEY": "k",
            "MINIO_SECRET_KEY": "s",
        }
        base.update(overrides)
        assert any(expected in e for e in self._production(**base)["errors"])

    def test_unknown_provider_is_an_error(self):
        assert any(
            "STORAGE_PROVIDER" in e for e in self._production(STORAGE_PROVIDER="dropbox")["errors"]
        )

    def test_azure_is_named_as_unimplemented(self):
        assert any(
            "not implemented" in e for e in self._production(STORAGE_PROVIDER="azure")["errors"]
        )

    def test_filesystem_in_production_warns_about_scaling(self):
        result = self._production(STORAGE_PROVIDER="filesystem")
        assert result["errors"] == [] or not any("STORAGE_PROVIDER" in e for e in result["errors"])
        assert any("filesystem" in w for w in result["warnings"])

    @pytest.mark.parametrize("seconds", [0, 10, 604801, -1])
    def test_absurd_signed_url_lifetimes_are_refused(self, seconds):
        errors = self._production(SIGNED_URL_EXPIRE_SECONDS=seconds)["errors"]
        assert any("SIGNED_URL_EXPIRE_SECONDS" in e for e in errors)

    def test_long_signed_url_lifetime_warns(self):
        result = self._production(SIGNED_URL_EXPIRE_SECONDS=86400)
        assert any("SIGNED_URL_EXPIRE_SECONDS" in w for w in result["warnings"])


# ------------------------------------------------------------- pipeline ----
class TestUploadStoresObjects:
    def test_upload_records_a_key_and_no_path(self, client, auth_headers, workspace_id, indexing):
        body = _upload(client, auth_headers, workspace_id)
        doc = _document(body["id"])

        assert doc.storage_key, "upload must record a storage key"
        assert doc.storage_key.startswith(f"{DOCUMENT_PREFIX}/{workspace_id}/")
        assert doc.storage_provider == "filesystem"
        assert doc.file_path is None, "no absolute path may be written any more"
        assert get_storage().exists(doc.storage_key)

    def test_upload_response_leaks_neither_key_nor_path(
        self, client, auth_headers, workspace_id, indexing
    ):
        body = _upload(client, auth_headers, workspace_id)
        doc = _document(body["id"])
        serialised = str(body)

        assert doc.storage_key not in serialised
        assert "storage_key" not in serialised
        assert str(settings.UPLOAD_DIR) not in serialised
        assert DOCUMENT_PREFIX not in serialised

    def test_document_list_leaks_no_storage_detail(
        self, client, auth_headers, workspace_id, indexing
    ):
        _upload(client, auth_headers, workspace_id)
        response = client.get(
            "/api/v1/documents", headers=auth_headers, params={"workspace_id": workspace_id}
        )
        assert response.status_code == 200
        body = response.text
        assert "storage_key" not in body
        assert "file_path" not in body
        assert str(Path(settings.UPLOAD_DIR).resolve()) not in body

    def test_upload_to_s3_stores_the_object_there(
        self, client, auth_headers, workspace_id, indexing, s3_backed_app
    ):
        body = _upload(client, auth_headers, workspace_id)
        doc = _document(body["id"])

        assert doc.storage_provider == "s3"
        assert s3_backed_app.exists(doc.storage_key)
        assert s3_backed_app.info(doc.storage_key).size > 0

    def test_quarantine_is_emptied_by_a_successful_upload(
        self, client, auth_headers, workspace_id, indexing
    ):
        quarantine = Path(settings.QUARANTINE_DIR)
        _upload(client, auth_headers, workspace_id)
        leftovers = [p for p in quarantine.iterdir() if p.is_file()] if quarantine.is_dir() else []
        assert leftovers == [], f"quarantine still holds {leftovers}"

    def test_storage_failure_rejects_the_upload_cleanly(
        self, client, auth_headers, workspace_id, indexing, monkeypatch
    ):
        """A storage outage must not create a row pointing at nothing."""
        from app.services.storage.filesystem import FilesystemStorage as FS

        monkeypatch.setattr(
            FS, "put", lambda *a, **k: (_ for _ in ()).throw(StorageError("bucket on fire"))
        )
        response = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": ("x.txt", b"content " * 50, "text/plain")},
            data={"workspace_id": workspace_id},
        )
        assert response.status_code == 503
        assert "storage" in _error_message(response).lower()
        # And nothing is left in quarantine to sweep up.
        quarantine = Path(settings.QUARANTINE_DIR)
        assert not [p for p in quarantine.iterdir() if p.is_file()]


class TestDownload:
    def test_filesystem_download_streams_the_original_file(
        self, client, auth_headers, workspace_id, indexing
    ):
        content = b"Downloadable content. " * 40
        body = _upload(client, auth_headers, workspace_id, content=content)

        response = client.get(f"/api/v1/documents/{body['id']}/download", headers=auth_headers)
        assert response.status_code == 200
        assert response.content == content
        assert "attachment" in response.headers.get("content-disposition", "")

    def test_download_exposes_no_server_path(self, client, auth_headers, workspace_id, indexing):
        body = _upload(client, auth_headers, workspace_id)
        response = client.get(f"/api/v1/documents/{body['id']}/download", headers=auth_headers)
        headers = str(response.headers)
        assert str(Path(settings.UPLOAD_DIR).resolve()) not in headers
        assert DOCUMENT_PREFIX not in headers

    def test_s3_download_redirects_to_a_signed_url(
        self, client, auth_headers, workspace_id, indexing, s3_backed_app
    ):
        body = _upload(client, auth_headers, workspace_id)
        response = client.get(
            f"/api/v1/documents/{body['id']}/download",
            headers=auth_headers,
            follow_redirects=False,
        )
        assert response.status_code == 307
        location = response.headers["location"]
        assert "X-Amz-Signature=" in location
        assert f"X-Amz-Expires={settings.SIGNED_URL_EXPIRE_SECONDS}" in location

    def test_download_requires_authentication(self, client, auth_headers, workspace_id, indexing):
        body = _upload(client, auth_headers, workspace_id)
        assert client.get(f"/api/v1/documents/{body['id']}/download").status_code == 401

    def test_another_users_document_is_not_downloadable(
        self, client, auth_headers, workspace_id, indexing
    ):
        body = _upload(client, auth_headers, workspace_id)
        outsider = _register_outsider(client)
        response = client.get(f"/api/v1/documents/{body['id']}/download", headers=outsider)
        assert response.status_code == 404, "an outsider must not learn the document exists"

    def test_unknown_document_is_not_found(self, client, auth_headers):
        response = client.get(f"/api/v1/documents/{uuid.uuid4()}/download", headers=auth_headers)
        assert response.status_code == 404

    def test_missing_object_is_reported_as_unavailable(
        self, client, auth_headers, workspace_id, indexing
    ):
        body = _upload(client, auth_headers, workspace_id)
        doc = _document(body["id"])
        get_storage().delete(doc.storage_key)

        response = client.get(f"/api/v1/documents/{body['id']}/download", headers=auth_headers)
        assert response.status_code == 404
        assert "unavailable" in _error_message(response).lower()

    def test_legacy_document_is_still_downloadable(
        self, client, auth_headers, workspace_id, indexing
    ):
        """A row written before Phase 6 carries a path, not a key."""
        from app.core.database import SessionLocal
        from app.models.document import Document

        body = _upload(client, auth_headers, workspace_id, content=b"legacy bytes " * 20)
        doc = _document(body["id"])
        storage = get_storage()

        # Rewrite the row into its pre-migration shape: file at the root of
        # the storage directory, absolute path recorded, no key.
        legacy_name = f"{uuid.uuid4().hex}.txt"
        os.replace(storage.local_path(doc.storage_key), str(storage.root / legacy_name))

        db = SessionLocal()
        try:
            row = db.query(Document).filter(Document.id == doc.id).first()
            row.storage_key = None
            row.storage_provider = None
            row.file_path = str(storage.root / legacy_name)
            db.commit()
        finally:
            db.close()

        response = client.get(f"/api/v1/documents/{body['id']}/download", headers=auth_headers)
        assert response.status_code == 200
        assert response.content == b"legacy bytes " * 20


class TestDelete:
    def test_delete_removes_the_stored_object(self, client, auth_headers, workspace_id, indexing):
        body = _upload(client, auth_headers, workspace_id)
        doc = _document(body["id"])
        key = doc.storage_key
        assert get_storage().exists(key)

        response = client.delete(f"/api/v1/documents/{body['id']}", headers=auth_headers)
        assert response.status_code == 200
        assert not get_storage().exists(key), "delete must not orphan the object"
        assert _document(body["id"]) is None

    def test_delete_removes_the_object_from_s3(
        self, client, auth_headers, workspace_id, indexing, s3_backed_app
    ):
        body = _upload(client, auth_headers, workspace_id)
        key = _document(body["id"]).storage_key
        assert s3_backed_app.exists(key)

        assert (
            client.delete(f"/api/v1/documents/{body['id']}", headers=auth_headers).status_code
            == 200
        )
        assert not s3_backed_app.exists(key)

    def test_delete_survives_an_already_missing_object(
        self, client, auth_headers, workspace_id, indexing
    ):
        body = _upload(client, auth_headers, workspace_id)
        get_storage().delete(_document(body["id"]).storage_key)

        response = client.delete(f"/api/v1/documents/{body['id']}", headers=auth_headers)
        assert response.status_code == 200, "a missing object must not block the delete"
        assert _document(body["id"]) is None


# ------------------------------------------------------------- cleanup -----
class TestStorageCleanup:
    @pytest.fixture(autouse=True)
    def _isolated_root(self, provider_settings, tmp_path):
        self.storage = provider_settings(
            STORAGE_PROVIDER="filesystem", FILESYSTEM_ROOT=str(tmp_path / "sweep")
        )
        yield

    @staticmethod
    def _age(path: str, hours: float) -> None:
        old = time.time() - hours * 3600
        os.utime(path, (old, old))

    def test_orphan_older_than_the_grace_period_is_removed(self, sample_file):
        from app.worker.tasks import cleanup_storage

        key = build_document_key("ws-orphan", "txt")
        self.storage.put(key, sample_file())
        self._age(self.storage.local_path(key), hours=48)

        stats = cleanup_storage()
        assert stats["orphans_deleted"] == 1
        assert stats["bytes_reclaimed"] > 0
        assert not self.storage.exists(key)

    def test_recent_object_is_left_alone(self, sample_file):
        """An upload writes the object before it commits the row."""
        from app.worker.tasks import cleanup_storage

        key = build_document_key("ws-fresh", "txt")
        self.storage.put(key, sample_file())

        stats = cleanup_storage()
        assert stats["orphans_deleted"] == 0
        assert stats["orphans_skipped_recent"] >= 1
        assert self.storage.exists(key)

    def test_live_document_is_never_swept(self, client, auth_headers, workspace_id, indexing):
        from app.worker.tasks import cleanup_storage

        body = _upload(client, auth_headers, workspace_id)
        key = _document(body["id"]).storage_key
        self._age(self.storage.local_path(key), hours=72)

        cleanup_storage()
        assert self.storage.exists(key), "an object with a live row must survive the sweep"

    def test_legacy_document_is_never_swept(self, sample_file):
        """A document uploaded before storage was abstracted must survive.

        Those objects sit flat at the root of the storage directory rather
        than under the document prefix, so two things have to hold: the sweep
        must not stray outside its prefix, and the key derived from the row's
        legacy ``file_path`` must count as live.
        """
        from app.core.database import SessionLocal
        from app.models.document import Document
        from app.worker.tasks import _live_keys, cleanup_storage

        legacy_name = f"{uuid.uuid4().hex}.txt"
        self.storage.put(legacy_name, sample_file())
        self._age(self.storage.local_path(legacy_name), hours=72)

        db = SessionLocal()
        try:
            row = db.query(Document).first()
            assert row is not None, "fixture ordering: expected an existing document row"
            document_id, saved_key, saved_path = row.id, row.storage_key, row.file_path
            row.storage_key = None
            row.file_path = f"/old/uploads/{legacy_name}"
            db.commit()

            assert legacy_name in _live_keys(db), "the legacy row must register as live"
        finally:
            db.close()
        try:
            cleanup_storage()
            assert self.storage.exists(legacy_name), "a legacy object must survive the sweep"
        finally:
            db = SessionLocal()
            try:
                restored = db.query(Document).filter(Document.id == document_id).first()
                restored.storage_key, restored.file_path = saved_key, saved_path
                db.commit()
            finally:
                db.close()

    def test_sweep_stays_inside_the_document_prefix(self, sample_file):
        """Nothing outside ``documents/`` is a candidate, whatever its age."""
        from app.worker.tasks import cleanup_storage

        outsider = f"other/{uuid.uuid4().hex}.bin"
        self.storage.put(outsider, sample_file())
        self._age(self.storage.local_path(outsider), hours=1000)

        cleanup_storage()
        assert self.storage.exists(outsider)

    def test_deletion_budget_caps_a_runaway_sweep(self, sample_file, monkeypatch):
        from app.worker.tasks import cleanup_storage

        monkeypatch.setattr(settings, "STORAGE_CLEANUP_MAX_DELETES", 2)
        for _ in range(5):
            key = build_document_key("ws-many", "txt")
            self.storage.put(key, sample_file())
            self._age(self.storage.local_path(key), hours=48)

        stats = cleanup_storage()
        assert stats["orphans_deleted"] == 2
        assert stats["orphans_over_budget"] == 3
        assert len(self.storage.list(f"{DOCUMENT_PREFIX}/ws-many")) == 3

    def test_abandoned_temp_files_are_removed(self, tmp_path, monkeypatch):
        from app.worker.tasks import cleanup_storage

        quarantine = tmp_path / "q"
        quarantine.mkdir()
        monkeypatch.setattr(settings, "QUARANTINE_DIR", str(quarantine))

        stale = quarantine / "abandoned.part"
        stale.write_bytes(b"half an upload")
        self._age(str(stale), hours=24)
        fresh = quarantine / "in-flight.part"
        fresh.write_bytes(b"still uploading")

        stats = cleanup_storage()
        assert stats["temp_files_deleted"] == 1
        assert not stale.exists()
        assert fresh.exists(), "a file from a request still running must survive"

    def test_failed_upload_bytes_are_purged_but_the_row_remains(self, sample_file, monkeypatch):
        from app.core.database import SessionLocal
        from app.models.document import Document, DocumentStatus
        from app.worker.tasks import cleanup_storage

        key = build_document_key("ws-failed", "txt")
        self.storage.put(key, sample_file())

        db = SessionLocal()
        try:
            doc = Document(
                workspace_id="ws-failed",
                filename="f.txt",
                original_filename="f.txt",
                file_type="txt",
                file_size=10,
                status=DocumentStatus.FAILED,
                storage_provider="filesystem",
                storage_key=key,
                processing_completed_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=60),
            )
            db.add(doc)
            db.commit()
            document_id = doc.id
        finally:
            db.close()

        try:
            stats = cleanup_storage()
            assert stats["failed_uploads_purged"] == 1
            assert not self.storage.exists(key)

            db = SessionLocal()
            try:
                row = db.query(Document).filter(Document.id == document_id).first()
                assert row is not None, "the failure record must survive the purge"
                assert row.storage_key is None
                assert row.storage_metadata.get("purged_at")
            finally:
                db.close()
        finally:
            db = SessionLocal()
            try:
                db.query(Document).filter(Document.id == document_id).delete()
                db.commit()
            finally:
                db.close()

    def test_recent_failure_is_kept(self, sample_file, monkeypatch):
        from app.worker.tasks import cleanup_storage

        monkeypatch.setattr(settings, "STORAGE_FAILED_RETENTION_DAYS", 0)
        key = build_document_key("ws-failed", "txt")
        self.storage.put(key, sample_file())
        self._age(self.storage.local_path(key), hours=1)

        assert cleanup_storage()["failed_uploads_purged"] == 0

    def test_one_failing_sweep_does_not_stop_the_others(self, monkeypatch):
        from app.worker import tasks

        monkeypatch.setattr(
            tasks,
            "_sweep_orphans",
            lambda *a: (_ for _ in ()).throw(StorageError("bucket unreachable")),
        )
        stats = tasks.cleanup_storage()
        assert stats["errors"] == ["orphans"]
        assert "temp_files_deleted" in stats


# ------------------------------------------------------------ regression ---
class TestNoRegression:
    def test_upload_response_contract_is_unchanged(
        self, client, auth_headers, workspace_id, indexing
    ):
        body = _upload(client, auth_headers, workspace_id)
        for field in (
            "id",
            "title",
            "fileType",
            "fileSizeKb",
            "status",
            "chunkCount",
            "tags",
            "uploadedBy",
            "uploadedAt",
            "version",
            "ocrApplied",
            "folderPath",
        ):
            assert field in body, f"upload response lost {field}"

    def test_duplicate_detection_still_short_circuits(
        self, client, auth_headers, workspace_id, indexing
    ):
        content = b"Exactly the same bytes, twice. " * 30
        name = f"{uuid.uuid4().hex}.txt"
        files = {"file": (name, content, "text/plain")}
        first = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files=files,
            data={"workspace_id": workspace_id},
        )
        second = client.post(
            "/api/v1/documents/upload",
            headers=auth_headers,
            files={"file": (name, content, "text/plain")},
            data={"workspace_id": workspace_id},
        )
        assert first.status_code == 200 and second.status_code == 200
        assert second.json().get("duplicate") is True
        assert second.json()["id"] == first.json()["id"]

    def test_status_endpoint_still_works(self, client, auth_headers, workspace_id, indexing):
        body = _upload(client, auth_headers, workspace_id)
        response = client.get(f"/api/v1/documents/{body['id']}/status", headers=auth_headers)
        assert response.status_code == 200
        assert set(response.json()) >= {"id", "status", "progress", "chunkCount"}

    def test_ingestion_reads_through_the_storage_layer(
        self, client, auth_headers, workspace_id, indexing
    ):
        """The worker must not depend on file_path, which is now NULL."""
        from app.worker.tasks import _document_file

        body = _upload(client, auth_headers, workspace_id, content=b"Indexable text. " * 50)
        doc = _document(body["id"])
        with _document_file(doc) as path:
            assert Path(path).read_bytes().startswith(b"Indexable text.")

    def test_ingestion_from_s3_fetches_a_local_copy(self, sample_file, s3_backed_app):
        from app.models.document import Document
        from app.worker.tasks import _document_file

        key = build_document_key("ws-1", "txt")
        s3_backed_app.put(key, sample_file(b"remote bytes"))
        doc = Document(id="fake", storage_key=key, file_path=None, file_type="txt")

        with _document_file(doc) as path:
            assert Path(path).read_bytes() == b"remote bytes"
            temp_copy = path
        assert not Path(temp_copy).exists(), "the temporary copy must be cleaned up"

    def test_ingestion_without_a_key_raises_not_found(self):
        from app.models.document import Document
        from app.worker.tasks import _document_file

        doc = Document(id="fake", storage_key=None, file_path=None, file_type="txt")
        with pytest.raises(ObjectNotFound):
            with _document_file(doc):
                pass
