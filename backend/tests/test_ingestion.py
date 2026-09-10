"""
Background document ingestion: the queued contract, the state machine and
the failure paths.

Celery is not faked here. Where a test needs an upload to stay queued rather
than being indexed on the spot, the app is pointed at kombu's in-memory
transport -- a real broker implementation -- so ``.delay()`` genuinely
publishes a message that no worker consumes. Where a test needs the task to
run, it is invoked through Celery's own ``apply()``, which executes the real
task with the real retry and failure handling.
"""

from unittest.mock import patch

import pytest

from app.models.document import DocumentStatus

pytestmark = pytest.mark.integration


@pytest.fixture
def queued_broker():
    """Point Celery at an in-memory broker so uploads stay queued.

    settings.REDIS_URL is set so the dispatcher takes the queued branch; the
    broker URL is the in-memory transport so nothing needs a Redis server.
    """
    from app.core.config import settings
    from app.worker.celery_app import celery_app

    original_url = settings.REDIS_URL
    original_broker = celery_app.conf.broker_url
    original_backend = celery_app.conf.result_backend

    settings.REDIS_URL = "redis://localhost:6379"
    celery_app.conf.broker_url = "memory://"
    celery_app.conf.result_backend = "cache+memory://"
    try:
        yield
    finally:
        settings.REDIS_URL = original_url
        celery_app.conf.broker_url = original_broker
        celery_app.conf.result_backend = original_backend


def _upload(client, headers, workspace_id, name="paper.txt", content=None):
    return client.post(
        "/api/v1/documents/upload",
        headers=headers,
        files={"file": (name, content or b"Ingestion corpus. " * 60, "text/plain")},
        data={"workspace_id": workspace_id},
    )


# ------------------------------------------------------------ the contract --
class TestUploadReturnsQueued:
    def test_upload_returns_immediately_as_queued(
        self, client, auth_headers, workspace_id, queued_broker
    ):
        response = _upload(client, auth_headers, workspace_id)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == DocumentStatus.QUEUED
        assert body["chunkCount"] == 0
        assert body["progress"] == 0

    def test_the_document_is_persisted_as_queued(
        self, client, auth_headers, workspace_id, queued_broker
    ):
        document_id = _upload(client, auth_headers, workspace_id).json()["id"]

        from app.core.database import SessionLocal
        from app.models.document import Document

        session = SessionLocal()
        try:
            doc = session.get(Document, document_id)
            assert doc.status == DocumentStatus.QUEUED
            assert doc.progress == 0
            assert doc.chunk_count == 0
            # Nothing has claimed it yet.
            assert doc.processing_started_at is None
            assert doc.processing_completed_at is None
        finally:
            session.close()

    def test_no_indexing_happened_during_the_request(
        self, client, auth_headers, workspace_id, queued_broker
    ):
        """The point of the change: the request does no pipeline work."""
        with patch("app.worker.tasks.embed_texts") as embed:
            _upload(client, auth_headers, workspace_id)
        embed.assert_not_called()

    def test_the_response_keeps_every_existing_field(
        self, client, auth_headers, workspace_id, queued_broker
    ):
        body = _upload(client, auth_headers, workspace_id).json()
        # Only `status` changes meaning; the rest of the contract is intact.
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


# -------------------------------------------------------- the status route --
class TestStatusEndpoint:
    def test_requires_authentication(self, client):
        assert client.get("/api/v1/documents/any-id/status").status_code == 401

    def test_reports_a_queued_document(self, client, auth_headers, workspace_id, queued_broker):
        document_id = _upload(client, auth_headers, workspace_id).json()["id"]
        body = client.get(f"/api/v1/documents/{document_id}/status", headers=auth_headers).json()

        assert body["status"] == DocumentStatus.QUEUED
        assert body["progress"] == 0
        assert body["chunkCount"] == 0
        assert body["error"] is None
        assert body["updatedAt"]

    def test_reports_an_indexed_document(self, client, auth_headers, workspace_id):
        # No queued_broker fixture: with no broker the dispatcher indexes
        # inline, so the document is already finished.
        with (
            patch("app.worker.tasks.embed_texts", lambda texts: [[0.1] * 384 for _ in texts]),
            patch(
                "app.worker.tasks.upsert_chunks",
                lambda **kw: [f"p-{i}" for i in range(len(kw["chunks"]))],
            ),
        ):
            document_id = _upload(client, auth_headers, workspace_id).json()["id"]

        body = client.get(f"/api/v1/documents/{document_id}/status", headers=auth_headers).json()
        assert body["status"] == DocumentStatus.INDEXED
        assert body["progress"] == 100
        assert body["chunkCount"] >= 1
        assert body["error"] is None
        assert body["startedAt"] and body["completedAt"]

    def test_unknown_document_is_404(self, client, auth_headers):
        response = client.get("/api/v1/documents/no-such-doc/status", headers=auth_headers)
        assert response.status_code == 404

    def test_another_users_document_is_404(self, client, auth_headers, workspace_id, queued_broker):
        from tests.conftest import STRONG_PASSWORD

        document_id = _upload(client, auth_headers, workspace_id).json()["id"]

        signup = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Outsider",
                "email": "status_outsider@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if signup.status_code != 200:
            signup = client.post(
                "/api/v1/auth/login",
                json={"email": "status_outsider@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

        # 404 rather than 403 so document ids cannot be probed.
        response = client.get(f"/api/v1/documents/{document_id}/status", headers=headers)
        assert response.status_code == 404


# ---------------------------------------------------------- the task itself --
class TestIngestTask:
    def _queued_document(self, client, auth_headers, workspace_id):
        return _upload(client, auth_headers, workspace_id).json()["id"]

    def test_the_task_drives_a_queued_document_to_indexed(
        self, client, auth_headers, workspace_id, queued_broker
    ):
        document_id = self._queued_document(client, auth_headers, workspace_id)

        from app.worker.tasks import process_document

        with (
            patch("app.worker.tasks.embed_texts", lambda texts: [[0.1] * 384 for _ in texts]),
            patch(
                "app.worker.tasks.upsert_chunks",
                lambda **kw: [f"p-{i}" for i in range(len(kw["chunks"]))],
            ),
        ):
            result = process_document.apply(args=[document_id]).get()

        assert result["status"] == DocumentStatus.INDEXED
        assert result["chunk_count"] >= 1

        body = client.get(f"/api/v1/documents/{document_id}/status", headers=auth_headers).json()
        assert body["status"] == DocumentStatus.INDEXED
        assert body["progress"] == 100

    def test_timestamps_bracket_the_work(self, client, auth_headers, workspace_id, queued_broker):
        document_id = self._queued_document(client, auth_headers, workspace_id)

        from app.worker.tasks import process_document

        with (
            patch("app.worker.tasks.embed_texts", lambda texts: [[0.1] * 384 for _ in texts]),
            patch(
                "app.worker.tasks.upsert_chunks",
                lambda **kw: [f"p-{i}" for i in range(len(kw["chunks"]))],
            ),
        ):
            process_document.apply(args=[document_id]).get()

        from app.core.database import SessionLocal
        from app.models.document import Document

        session = SessionLocal()
        try:
            doc = session.get(Document, document_id)
            assert doc.processing_started_at is not None
            assert doc.processing_completed_at is not None
            assert doc.processing_completed_at >= doc.processing_started_at
        finally:
            session.close()

    def test_a_missing_document_is_not_an_error(self):
        """A document deleted between enqueue and pickup must not crash."""
        from app.worker.tasks import process_document

        result = process_document.apply(args=["deleted-before-pickup"]).get()
        assert result["status"] == "missing"

    def test_an_already_indexed_document_is_skipped(self, client, auth_headers, workspace_id):
        """Redelivery after acks_late must not re-index."""
        with (
            patch("app.worker.tasks.embed_texts", lambda texts: [[0.1] * 384 for _ in texts]),
            patch(
                "app.worker.tasks.upsert_chunks",
                lambda **kw: [f"p-{i}" for i in range(len(kw["chunks"]))],
            ),
        ):
            document_id = _upload(client, auth_headers, workspace_id).json()["id"]

        from app.worker.tasks import process_document

        with patch("app.worker.tasks.embed_texts") as embed:
            result = process_document.apply(args=[document_id]).get()
        embed.assert_not_called()
        assert result["status"] == DocumentStatus.INDEXED

    def test_a_retry_does_not_duplicate_chunks(self, client, auth_headers, workspace_id):
        """Re-running ingestion replaces chunks rather than appending."""
        stub_embed = lambda texts: [[0.1] * 384 for _ in texts]  # noqa: E731
        stub_upsert = lambda **kw: [f"p-{i}" for i in range(len(kw["chunks"]))]  # noqa: E731

        with (
            patch("app.worker.tasks.embed_texts", stub_embed),
            patch("app.worker.tasks.upsert_chunks", stub_upsert),
        ):
            document_id = _upload(client, auth_headers, workspace_id).json()["id"]

        from app.core.database import SessionLocal
        from app.models.document import Document, DocumentChunk

        session = SessionLocal()
        try:
            first = (
                session.query(DocumentChunk)
                .filter(DocumentChunk.document_id == document_id)
                .count()
            )
            # Put it back to queued so the task will run again.
            doc = session.get(Document, document_id)
            doc.status = DocumentStatus.QUEUED
            session.commit()
        finally:
            session.close()

        from app.worker.tasks import process_document

        with (
            patch("app.worker.tasks.embed_texts", stub_embed),
            patch("app.worker.tasks.upsert_chunks", stub_upsert),
        ):
            process_document.apply(args=[document_id]).get()

        session = SessionLocal()
        try:
            second = (
                session.query(DocumentChunk)
                .filter(DocumentChunk.document_id == document_id)
                .count()
            )
            assert second == first, "a re-run duplicated the document's chunks"
        finally:
            session.close()


# ------------------------------------------------------------- the reaper ---
class TestStalledDocumentReaper:
    def test_a_document_whose_worker_died_is_failed(
        self, client, auth_headers, workspace_id, queued_broker
    ):
        """A killed worker runs no handler, so nothing else would free this."""
        from datetime import datetime, timedelta

        document_id = _upload(client, auth_headers, workspace_id).json()["id"]

        from app.core.database import SessionLocal
        from app.models.document import Document
        from app.worker.tasks import STALLED_AFTER, reap_stalled_documents

        session = SessionLocal()
        try:
            doc = session.get(Document, document_id)
            doc.status = DocumentStatus.PROCESSING
            doc.processing_started_at = datetime.utcnow() - STALLED_AFTER - timedelta(minutes=5)
            session.commit()
        finally:
            session.close()

        assert reap_stalled_documents.apply().get()["reaped"] >= 1

        body = client.get(f"/api/v1/documents/{document_id}/status", headers=auth_headers).json()
        assert body["status"] == DocumentStatus.FAILED
        assert "stopped responding" in (body["error"] or "")

    def test_a_recently_started_document_is_left_alone(
        self, client, auth_headers, workspace_id, queued_broker
    ):
        from datetime import datetime

        document_id = _upload(client, auth_headers, workspace_id).json()["id"]

        from app.core.database import SessionLocal
        from app.models.document import Document
        from app.worker.tasks import reap_stalled_documents

        session = SessionLocal()
        try:
            doc = session.get(Document, document_id)
            doc.status = DocumentStatus.PROCESSING
            doc.processing_started_at = datetime.utcnow()
            session.commit()
        finally:
            session.close()

        reap_stalled_documents.apply().get()

        body = client.get(f"/api/v1/documents/{document_id}/status", headers=auth_headers).json()
        # Still working: reaping it would kill a live ingest.
        assert body["status"] == DocumentStatus.PROCESSING


# ------------------------------------------------------------- dispatching --
class TestDispatch:
    def test_no_broker_falls_back_to_inline_processing(self):
        from app.worker import dispatch

        assert dispatch.broker_available() is False

    def test_a_broker_is_detected_when_redis_is_configured(self, queued_broker):
        from app.worker import dispatch

        assert dispatch.broker_available() is True

    def test_a_broker_failure_does_not_fail_the_upload(
        self, client, auth_headers, workspace_id, queued_broker
    ):
        """An accepted upload must not be lost because the broker blinked."""
        from app.worker import tasks

        with (
            patch.object(
                tasks.process_document, "delay", side_effect=OSError("broker unreachable")
            ),
            patch("app.worker.tasks.embed_texts", lambda texts: [[0.1] * 384 for _ in texts]),
            patch(
                "app.worker.tasks.upsert_chunks",
                lambda **kw: [f"p-{i}" for i in range(len(kw["chunks"]))],
            ),
        ):
            response = _upload(client, auth_headers, workspace_id)

        assert response.status_code == 200
        body = client.get(
            f"/api/v1/documents/{response.json()['id']}/status", headers=auth_headers
        ).json()
        # It fell back to inline processing rather than dropping the document.
        assert body["status"] == DocumentStatus.INDEXED


class TestCeleryConfiguration:
    def test_reliability_settings_are_set(self):
        from app.worker.celery_app import celery_app

        conf = celery_app.conf
        # Without these a killed worker silently loses the document.
        assert conf.task_acks_late is True
        assert conf.task_reject_on_worker_lost is True
        assert conf.worker_prefetch_multiplier == 1
        assert conf.task_time_limit > conf.task_soft_time_limit

    def test_broker_and_results_use_separate_redis_databases(self):
        from app.worker.celery_app import RESULT_DB, _redis_db_url

        # A FLUSHDB against the cache must not destroy queued work.
        assert _redis_db_url("redis://cache:6379/0", RESULT_DB).endswith(f"/{RESULT_DB}")
        assert _redis_db_url("redis://cache:6379", 1) == "redis://cache:6379/1"
