"""
Document upload, indexing and deletion end to end.

The upload route is the longest path in the application and touches every
security control: extension allowlist, content sniffing, size cap, antivirus,
quarantine, deduplication, then extraction, chunking, embedding and vector
upsert. Only the embedding model and Qdrant are substituted -- the whole
validation and persistence chain is real.
"""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def indexing_stubs():
    """Substitute the embedding model and Qdrant, leaving everything else real."""
    with patch("app.api.v1.documents.embed_texts") as embed, patch(
        "app.api.v1.documents.upsert_chunks"
    ) as upsert:
        embed.side_effect = lambda texts: [[0.1] * 384 for _ in texts]
        upsert.side_effect = lambda chunks, embeddings, document_id, workspace_id: [
            f"point-{i}" for i in range(len(chunks))
        ]
        yield {"embed": embed, "upsert": upsert}


def _upload(client, headers, name="report.txt", content=None, ctype="text/plain", **data):
    return client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": (name, content or b"Research content. " * 60, ctype)},
        data=data,
    )


# ----------------------------------------------------------- happy path ----
class TestUploadSucceeds:
    def test_document_is_indexed(self, client, auth_headers, workspace_id, indexing_stubs):
        response = _upload(client, auth_headers, workspace_id=workspace_id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "indexed"
        assert body["chunkCount"] >= 1
        assert body["title"] == "report.txt"

    def test_response_uses_the_frontend_field_names(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        body = _upload(client, auth_headers, workspace_id=workspace_id).json()
        assert {
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
        } <= set(body)

    def test_chunks_are_embedded_and_upserted_under_the_callers_workspace(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        _upload(client, auth_headers, workspace_id=workspace_id)
        upsert = indexing_stubs["upsert"]
        upsert.assert_called_once()
        assert upsert.call_args.kwargs["workspace_id"] == workspace_id

    def test_chunk_rows_are_persisted_and_linked_to_vector_points(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        document_id = _upload(client, auth_headers, workspace_id=workspace_id).json()["id"]

        from app.core.database import SessionLocal
        from app.models.document import DocumentChunk

        session = SessionLocal()
        try:
            chunks = (
                session.query(DocumentChunk)
                .filter(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index)
                .all()
            )
            assert chunks
            assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
            # Without the point id a deleted chunk leaves an orphaned vector.
            assert all(c.qdrant_point_id for c in chunks)
        finally:
            session.close()

    def test_content_hash_and_mime_type_are_recorded(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        document_id = _upload(client, auth_headers, workspace_id=workspace_id).json()["id"]

        from app.core.database import SessionLocal
        from app.models.document import Document

        session = SessionLocal()
        try:
            doc = session.get(Document, document_id)
            assert len(doc.content_hash) == 64  # sha-256, hex
            assert doc.mime_type
            assert doc.indexed_at is not None
        finally:
            session.close()

    def test_folder_defaults_when_not_supplied(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        body = _upload(client, auth_headers, workspace_id=workspace_id).json()
        assert body["folderPath"] == "/Uploads"

    def test_folder_is_honoured_when_supplied(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        body = _upload(
            client, auth_headers, workspace_id=workspace_id, folder="/Research/2026"
        ).json()
        assert body["folderPath"] == "/Research/2026"

    def test_markdown_and_csv_are_accepted(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        for name, content, ctype in (
            ("notes.md", b"# Heading\n\n" + b"body text " * 50, "text/markdown"),
            ("rows.csv", b"col_a,col_b\n" + b"1,2\n" * 200, "text/csv"),
        ):
            response = _upload(
                client, auth_headers, name=name, content=content, ctype=ctype,
                workspace_id=workspace_id,
            )
            assert response.status_code == 200, f"{name}: {response.text}"


# ------------------------------------------------------------ rejections ----
class TestUploadRejections:
    def test_upload_requires_authentication(self, client):
        response = client.post(
            "/api/v1/documents/upload", files={"file": ("a.txt", b"x", "text/plain")}
        )
        assert response.status_code == 401

    def test_blocked_extension_is_refused(self, client, auth_headers, workspace_id):
        response = _upload(
            client, auth_headers, name="payload.exe",
            content=b"MZ\x90\x00" + b"\x00" * 400,
            ctype="application/octet-stream", workspace_id=workspace_id,
        )
        assert response.status_code == 415

    def test_extension_disguise_is_caught_by_content_sniffing(
        self, client, auth_headers, workspace_id
    ):
        # A PE binary renamed to .txt: the allowlist passes, content does not.
        response = _upload(
            client, auth_headers, name="innocent.txt",
            content=b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 500,
            workspace_id=workspace_id,
        )
        assert response.status_code == 415

    def test_oversized_upload_is_refused(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        from app.core.config import settings

        original = settings.MAX_UPLOAD_SIZE_MB
        # max_upload_bytes floors at 1 MB, so the cap cannot be set lower.
        settings.MAX_UPLOAD_SIZE_MB = 1
        try:
            response = _upload(
                client, auth_headers, content=b"x" * (2 * 1024 * 1024),
                workspace_id=workspace_id,
            )
            assert response.status_code == 413
            # The cap must be enforced mid-write, not after buffering it all.
            indexing_stubs["upsert"].assert_not_called()
        finally:
            settings.MAX_UPLOAD_SIZE_MB = original

    def test_infected_file_is_quarantined_and_refused(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        from app.services.antivirus import ScanResult

        infected = ScanResult(
            is_clean=False, scanner="clamav", scanned=True, threat="Eicar-Test-Signature"
        )
        with patch("app.api.v1.documents.get_scanner") as scanner:
            scanner.return_value.scan_file.return_value = infected
            response = _upload(client, auth_headers, workspace_id=workspace_id)

        assert response.status_code == 400
        # Errors are wrapped in the standard envelope by the exception handler.
        assert "security scan" in response.json()["error"]["message"]
        # Nothing may reach the index.
        indexing_stubs["upsert"].assert_not_called()
        # The signature must not be echoed to the uploader.
        assert "Eicar-Test-Signature" not in response.text

    def test_rejected_upload_leaves_no_file_behind(self, client, auth_headers, workspace_id):
        from pathlib import Path

        from app.core.config import settings

        quarantine = Path(settings.QUARANTINE_DIR)
        before = set(quarantine.glob("*")) if quarantine.exists() else set()

        _upload(
            client, auth_headers, name="bad.txt",
            content=b"MZ\x90\x00" + b"\x00" * 500, workspace_id=workspace_id,
        )

        after = set(quarantine.glob("*")) if quarantine.exists() else set()
        # A quarantine that accumulates rejected uploads fills the disk.
        assert after == before

    def test_upload_into_another_users_workspace_is_refused(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        from tests.conftest import STRONG_PASSWORD

        signup = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Outsider",
                "email": "doc_outsider@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if signup.status_code != 200:
            signup = client.post(
                "/api/v1/auth/login",
                json={"email": "doc_outsider@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

        response = _upload(client, headers, workspace_id=workspace_id)
        assert response.status_code in (400, 403, 404)


# --------------------------------------------------------- deduplication ----
class TestDuplicateDetection:
    def test_identical_content_is_not_reindexed(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        content = b"Exactly the same bytes every time. " * 40
        first = _upload(client, auth_headers, content=content, workspace_id=workspace_id)
        assert first.status_code == 200

        second = _upload(
            client, auth_headers, name="copy.txt", content=content, workspace_id=workspace_id
        )
        assert second.status_code == 200
        body = second.json()
        assert body["duplicate"] is True
        assert body["id"] == first.json()["id"]
        # Re-embedding a known document wastes the most expensive step.
        assert indexing_stubs["upsert"].call_count == 1

    def test_different_content_is_indexed_separately(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        first = _upload(client, auth_headers, content=b"first body " * 40,
                        workspace_id=workspace_id)
        second = _upload(client, auth_headers, name="b.txt", content=b"second body " * 40,
                         workspace_id=workspace_id)
        assert first.json()["id"] != second.json()["id"]
        assert indexing_stubs["upsert"].call_count == 2

    def test_duplicate_detection_is_scoped_to_the_workspace(
        self, client, auth_headers, indexing_stubs
    ):
        content = b"Shared across workspaces. " * 40
        workspaces = client.get("/api/v1/workspaces", headers=auth_headers).json()
        first_ws = workspaces[0]["id"]

        created = client.post(
            "/api/v1/workspaces", headers=auth_headers, json={"name": "Second Workspace"}
        )
        assert created.status_code in (200, 201), created.text
        second_ws = created.json()["id"]

        a = _upload(client, auth_headers, content=content, workspace_id=first_ws)
        b = _upload(client, auth_headers, content=content, workspace_id=second_ws)

        # The same bytes in a different workspace are a different document.
        assert a.json()["id"] != b.json()["id"]
        assert b.json().get("duplicate") is not True

    def test_detection_can_be_disabled(self, client, auth_headers, workspace_id, indexing_stubs):
        from app.core.config import settings

        content = b"Duplicate detection off. " * 40
        original = settings.ENABLE_DUPLICATE_DETECTION
        settings.ENABLE_DUPLICATE_DETECTION = False
        try:
            a = _upload(client, auth_headers, content=content, workspace_id=workspace_id)
            b = _upload(client, auth_headers, content=content, workspace_id=workspace_id)
            assert a.json()["id"] != b.json()["id"]
        finally:
            settings.ENABLE_DUPLICATE_DETECTION = original


# ------------------------------------------------------ processing failure --
class TestProcessingFailure:
    def test_embedding_failure_marks_the_document_failed(
        self, client, auth_headers, workspace_id
    ):
        with patch("app.api.v1.documents.embed_texts", side_effect=RuntimeError("model down")):
            response = _upload(client, auth_headers, workspace_id=workspace_id)

        assert response.status_code == 500

        from app.core.database import SessionLocal
        from app.models.document import Document

        session = SessionLocal()
        try:
            doc = (
                session.query(Document)
                .filter(Document.status == "failed")
                .order_by(Document.created_at.desc())
                .first()
            )
            assert doc is not None
            # The reason must be recorded, not just the status.
            assert "model down" in (doc.error_message or "")
        finally:
            session.close()

    def test_vector_upsert_failure_is_surfaced(self, client, auth_headers, workspace_id):
        with patch("app.api.v1.documents.embed_texts", lambda texts: [[0.1] * 384 for _ in texts]), patch(
            "app.api.v1.documents.upsert_chunks", side_effect=RuntimeError("qdrant refused")
        ):
            response = _upload(client, auth_headers, workspace_id=workspace_id)
        assert response.status_code == 500

    def test_failed_upload_is_counted(self, client, auth_headers, workspace_id):
        with patch("app.api.v1.documents.embed_texts", side_effect=RuntimeError("boom")):
            _upload(client, auth_headers, workspace_id=workspace_id)
        assert 'researchsphere_uploads_total{outcome="failed"}' in client.get("/metrics").text


# ---------------------------------------------------------------- listing ---
class TestDocumentListing:
    def test_listing_requires_authentication(self, client):
        assert client.get("/api/v1/documents").status_code == 401

    def test_uploaded_document_appears_in_the_listing(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        document_id = _upload(client, auth_headers, workspace_id=workspace_id).json()["id"]
        listed = client.get("/api/v1/documents", headers=auth_headers).json()
        assert any(item["id"] == document_id for item in listed)

    def test_documents_are_not_visible_across_accounts(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        from tests.conftest import STRONG_PASSWORD

        _upload(client, auth_headers, workspace_id=workspace_id)

        signup = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Nosy",
                "email": "doc_isolation@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if signup.status_code != 200:
            signup = client.post(
                "/api/v1/auth/login",
                json={"email": "doc_isolation@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}
        assert client.get("/api/v1/documents", headers=headers).json() == []


# --------------------------------------------------------------- deletion ---
class TestDocumentDeletion:
    def test_owner_can_delete(self, client, auth_headers, workspace_id, indexing_stubs):
        document_id = _upload(client, auth_headers, workspace_id=workspace_id).json()["id"]
        with patch("app.api.v1.documents.delete_document_vectors") as drop:
            response = client.delete(f"/api/v1/documents/{document_id}", headers=auth_headers)
        assert response.status_code == 200
        drop.assert_called_once_with(document_id)

        assert not any(
            item["id"] == document_id
            for item in client.get("/api/v1/documents", headers=auth_headers).json()
        )

    def test_local_file_is_removed(self, client, auth_headers, workspace_id, indexing_stubs):
        import os

        document_id = _upload(client, auth_headers, workspace_id=workspace_id).json()["id"]

        from app.core.database import SessionLocal
        from app.models.document import Document

        session = SessionLocal()
        try:
            path = session.get(Document, document_id).file_path
        finally:
            session.close()
        assert os.path.exists(path)

        with patch("app.api.v1.documents.delete_document_vectors"):
            client.delete(f"/api/v1/documents/{document_id}", headers=auth_headers)
        # Orphaned files on disk are a slow storage leak.
        assert not os.path.exists(path)

    def test_another_users_document_cannot_be_deleted(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        from tests.conftest import STRONG_PASSWORD

        document_id = _upload(client, auth_headers, workspace_id=workspace_id).json()["id"]

        signup = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Attacker",
                "email": "doc_attacker@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if signup.status_code != 200:
            signup = client.post(
                "/api/v1/auth/login",
                json={"email": "doc_attacker@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

        response = client.delete(f"/api/v1/documents/{document_id}", headers=headers)
        # 404 rather than 403: existence must not be probeable by id.
        assert response.status_code == 404

        # ...and it is still there for its owner.
        assert any(
            item["id"] == document_id
            for item in client.get("/api/v1/documents", headers=auth_headers).json()
        )

    def test_unknown_document_returns_404(self, client, auth_headers):
        response = client.delete("/api/v1/documents/no-such-id", headers=auth_headers)
        assert response.status_code == 404

    def test_vector_deletion_failure_does_not_block_the_delete(
        self, client, auth_headers, workspace_id, indexing_stubs
    ):
        document_id = _upload(client, auth_headers, workspace_id=workspace_id).json()["id"]
        with patch(
            "app.api.v1.documents.delete_document_vectors",
            side_effect=RuntimeError("qdrant down"),
        ):
            response = client.delete(f"/api/v1/documents/{document_id}", headers=auth_headers)
        # Orphaned vectors are recoverable; a document that cannot be deleted
        # is not, so Qdrant being down must not block the request.
        assert response.status_code == 200

    def test_deletion_requires_authentication(self, client):
        assert client.delete("/api/v1/documents/some-id").status_code == 401
