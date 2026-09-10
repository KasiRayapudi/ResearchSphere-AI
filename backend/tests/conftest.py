"""
Shared pytest fixtures.

The suite runs with no external services: the database is a throwaway SQLite
file, Redis is replaced by fakeredis, and nothing here contacts Qdrant or
Gemini. Endpoints that genuinely need those degrade rather than fail, which is
behaviour worth testing in its own right.
"""

import os
import tempfile
import uuid
from pathlib import Path

import pytest

# Environment must be configured before the application is imported: the
# database engine is created at import time from these settings.
_TMP = Path(tempfile.mkdtemp(prefix="rs_tests_"))

# check_same_thread=false is required because the health endpoints run their
# database check in a worker thread via run_in_threadpool.
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}?check_same_thread=false"
os.environ["ENVIRONMENT"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough-for-validation-32"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["QUARANTINE_DIR"] = str(_TMP / "uploads" / ".quarantine")
os.environ["REDIS_URL"] = ""  # disabled unless a test opts in
os.environ["GEMINI_API_KEY"] = ""  # absent: RAG paths must degrade
os.environ["TRUSTED_HOSTS"] = "localhost,127.0.0.1,testserver"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

#: Satisfies the backend password policy; reused across tests.
STRONG_PASSWORD = "Xk9#mQp2$vLw7"


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    """Build the test schema by running the real migrations.

    Deliberately not Base.metadata.create_all: that would test the models
    against a schema no deployment ever gets. Running `alembic upgrade head`
    here means every test executes against the schema a fresh install
    actually receives, so a migration that does not match the models fails
    the suite rather than production.
    """
    from alembic import command
    from app.core.schema import alembic_config

    command.upgrade(alembic_config(), "head")
    yield


@pytest.fixture(scope="session")
def client():
    """TestClient with the application lifespan running."""
    with TestClient(main.app) as test_client:
        yield test_client


def _rate_limiter():
    """Locate the live RateLimitingMiddleware instance in the built stack."""
    from app.core.middleware import RateLimitingMiddleware

    node = getattr(main.app, "middleware_stack", None)
    for _ in range(20):
        if node is None:
            return None
        if isinstance(node, RateLimitingMiddleware):
            return node
        node = getattr(node, "app", None)
    return None


@pytest.fixture(autouse=True)
def _reset_rate_limiter(client):
    """Give every test a fresh rate-limit quota.

    The limiter is per-process and keyed by client IP, so without this the
    whole suite shares one 20-requests-per-minute auth budget and later tests
    fail with 429 for reasons that have nothing to do with what they assert.
    Tests that deliberately exercise rate limiting reset it themselves.
    """
    limiter = _rate_limiter()
    if limiter is not None:
        limiter._buckets.clear()
    yield


@pytest.fixture
def unique_email():
    return f"user_{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def registered_user(client, unique_email):
    """Create an account and return its credentials and tokens."""
    response = client.post(
        "/api/v1/auth/signup",
        json={"name": "Test User", "email": unique_email, "password": STRONG_PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return {
        "email": unique_email,
        "password": STRONG_PASSWORD,
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "user": payload["user"],
        "headers": {"Authorization": f"Bearer {payload['access_token']}"},
    }


@pytest.fixture
def auth_headers(registered_user):
    return registered_user["headers"]


@pytest.fixture
def workspace_id(client, auth_headers):
    """The workspace created automatically at signup."""
    response = client.get("/api/v1/workspaces", headers=auth_headers)
    assert response.status_code == 200
    workspaces = response.json()
    assert workspaces, "signup should create a default workspace"
    return workspaces[0]["id"]


@pytest.fixture
def admin_user(client, unique_email):
    """A user promoted to the admin role directly in the database."""
    response = client.post(
        "/api/v1/auth/signup",
        json={"name": "Admin User", "email": unique_email, "password": STRONG_PASSWORD},
    )
    assert response.status_code == 200

    from app.core.database import SessionLocal
    from app.models.user import User

    session = SessionLocal()
    try:
        user = session.query(User).filter(User.email == unique_email).first()
        user.role = "admin"
        session.commit()
    finally:
        session.close()

    # Re-authenticate so the token carries the elevated role.
    login = client.post(
        "/api/v1/auth/login", json={"email": unique_email, "password": STRONG_PASSWORD}
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    return {"email": unique_email, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def fake_redis():
    """Install a fakeredis client for tests that exercise Redis-backed paths."""
    import fakeredis

    from app.core import redis_client
    from app.core.config import settings

    original_url = settings.REDIS_URL
    settings.REDIS_URL = "redis://localhost:6379"
    redis_client.reset()
    server = fakeredis.FakeStrictRedis(decode_responses=True)
    redis_client.set_client(server)
    yield server
    redis_client.reset()
    settings.REDIS_URL = original_url


@pytest.fixture
def indexing():
    """Substitute the embedding model and Qdrant for upload tests.

    With no broker configured the dispatcher indexes inline, so an upload in
    a test runs the real pipeline unless these are replaced.
    """
    from unittest.mock import patch

    with (
        patch("app.worker.tasks.embed_texts", lambda texts: [[0.1] * 384 for _ in texts]),
        patch(
            "app.worker.tasks.upsert_chunks",
            lambda **kw: [f"point-{i}" for i in range(len(kw["chunks"]))],
        ),
    ):
        yield


@pytest.fixture
def text_file_bytes():
    return b"ResearchSphere automated test document. " * 40
