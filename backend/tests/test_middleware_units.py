"""
Rate limiter internals, security dependencies, analytics and password reset.

The rate limiter is tested at the unit level because its interesting states --
Redis failing mid-check, the local bucket refilling, the sweep evicting idle
entries -- cannot be reached reliably through the HTTP client.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def limiter():
    from app.core.middleware import RateLimitingMiddleware

    instance = RateLimitingMiddleware(app=MagicMock())
    instance._buckets.clear()
    return instance


# ------------------------------------------------------------ rate limiter --
class TestLocalBucket:
    def test_requests_are_allowed_up_to_the_limit(self, limiter):
        allowed = [limiter._check_local("client-a", 5)[0] for _ in range(5)]
        assert all(allowed)

    def test_the_next_request_is_blocked(self, limiter):
        for _ in range(5):
            limiter._check_local("client-b", 5)
        allowed, remaining, retry_after = limiter._check_local("client-b", 5)
        assert allowed is False
        assert remaining == 0
        assert retry_after == 60

    def test_remaining_counts_down(self, limiter):
        first = limiter._check_local("client-c", 10)[1]
        second = limiter._check_local("client-c", 10)[1]
        assert second < first

    def test_clients_have_independent_budgets(self, limiter):
        for _ in range(5):
            limiter._check_local("client-d", 5)
        # One noisy client must not lock everyone else out.
        assert limiter._check_local("client-e", 5)[0] is True

    def test_the_bucket_refills_over_time(self, limiter):
        for _ in range(5):
            limiter._check_local("client-f", 5)
        assert limiter._check_local("client-f", 5)[0] is False

        # Rewind the refill clock by half a minute rather than sleeping.
        limiter._buckets["client-f"]["last_refill"] -= 30
        assert limiter._check_local("client-f", 5)[0] is True

    def test_idle_buckets_are_evicted(self, limiter):
        limiter._check_local("stale-client", 5)
        limiter._buckets["stale-client"]["last_refill"] -= 400
        limiter._last_sweep = 0

        limiter._check_local("fresh-client", 5)
        # An unbounded bucket store is a memory leak keyed by client IP.
        assert "stale-client" not in limiter._buckets

    def test_the_store_is_capped(self, limiter):
        limiter._last_sweep = 0
        for index in range(limiter._MAX_LOCAL_BUCKETS + 200):
            limiter._buckets[f"client-{index}"] = {"tokens": 1.0, "last_refill": time.time()}
        limiter._last_sweep = 0
        limiter._check_local("trigger-sweep", 5)
        assert len(limiter._buckets) <= limiter._MAX_LOCAL_BUCKETS + 1

    def test_sweeping_is_throttled(self, limiter):
        limiter._check_local("client-g", 5)
        limiter._last_sweep = time.time()
        limiter._buckets["client-g"]["last_refill"] -= 400
        limiter._check_local("client-h", 5)
        # Sweeping on every request would scan the whole store per request.
        assert "client-g" in limiter._buckets


class TestRedisWindow:
    def _client(self, count):
        client = MagicMock()
        client.pipeline.return_value.execute.return_value = [None, None, count, None]
        return client

    def test_requests_under_the_limit_are_allowed(self, limiter):
        allowed, remaining, _ = limiter._check_redis(self._client(3), "key", 10)
        assert allowed is True
        assert remaining == 7

    def test_requests_over_the_limit_are_blocked(self, limiter):
        allowed, remaining, retry_after = limiter._check_redis(self._client(11), "key", 10)
        assert allowed is False
        assert remaining == 0
        assert retry_after == 60

    def test_a_blocked_request_is_removed_from_its_own_window(self, limiter):
        client = self._client(11)
        limiter._check_redis(client, "key", 10)
        # Otherwise a caller that keeps retrying extends its own lockout.
        client.zremrangebyrank.assert_called_once()

    def test_the_window_key_is_given_an_expiry(self, limiter):
        client = self._client(1)
        limiter._check_redis(client, "key", 10)
        pipeline = client.pipeline.return_value
        pipeline.expire.assert_called_once()

    def test_cleanup_of_the_old_window_is_requested(self, limiter):
        client = self._client(1)
        limiter._check_redis(client, "key", 10)
        client.pipeline.return_value.zremrangebyscore.assert_called_once()

    def test_a_failure_to_trim_does_not_raise(self, limiter):
        client = self._client(11)
        client.zremrangebyrank.side_effect = OSError("connection reset")
        # Best-effort cleanup: the decision has already been made.
        assert limiter._check_redis(client, "key", 10)[0] is False


class TestLimiterDegradation:
    @pytest.mark.asyncio
    async def test_redis_failure_falls_back_to_the_local_bucket(self, limiter):
        from starlette.responses import PlainTextResponse

        request = MagicMock()
        request.url.path = "/api/v1/workspaces"
        request.client.host = "1.2.3.4"

        async def _next(_):
            return PlainTextResponse("ok")

        failing = MagicMock()
        failing.pipeline.return_value.execute.side_effect = OSError("connection reset")

        with (
            patch("app.core.middleware.get_redis", return_value=failing),
            patch("app.core.middleware.mark_unavailable") as marked,
        ):
            response = await limiter.dispatch(request, _next)

        # A Redis outage must degrade to per-process limiting, not to a 500.
        assert response.status_code == 200
        marked.assert_called_once()
        assert limiter._buckets


# -------------------------------------------------------------- analytics ---
class TestAnalytics:
    pytestmark = pytest.mark.integration

    def test_analytics_require_authentication(self, client):
        assert client.get("/api/v1/analytics").status_code == 401

    def test_a_new_workspace_reports_zeroes_not_placeholders(
        self, client, auth_headers, workspace_id
    ):
        """Regression: the overview used to fall back to invented figures.

        `or 42`, `or 180` and `or 12.8` meant an empty workspace reported 42
        documents, a 180 ms latency and 12.8 GB of storage.
        """
        body = client.get(
            f"/api/v1/analytics?workspace_id={workspace_id}", headers=auth_headers
        ).json()

        # Every measured figure must be zero. storageCapacityMb is configured
        # quota, not a measurement, so it is excluded.
        measured = {
            "questionsAskedTotal",
            "documentsIndexedTotal",
            "embeddingsGeneratedTotal",
            "avgResponseTimeMs",
            "storageUsageMb",
        }
        assert measured <= set(body), body
        assert all(body[key] == 0 for key in measured), body

    def test_trend_series_is_flat_for_a_new_workspace(self, client, auth_headers, workspace_id):
        body = client.get(
            f"/api/v1/analytics?workspace_id={workspace_id}", headers=auth_headers
        ).json()
        series = body.get("dailyQueries") or []
        # A seeded 5 + i*2 curve used to be returned here.
        assert series, "the trend window should still be returned"
        assert all(point["queryCount"] == 0 for point in series), series
        assert all(point["avgLatencyMs"] == 0 for point in series), series

    def test_counts_reflect_real_activity(self, client, auth_headers, workspace_id):
        import json as _json

        frame = (
            "__SOURCES_JSON__"
            + _json.dumps({"__sources__": [], "__response_time_ms__": 10})
            + "__END_SOURCES__"
        )

        async def _stream(question, workspace_id):
            yield "answer"
            yield frame

        with patch("app.api.v1.chat.stream_rag_response", _stream):
            client.post(
                "/api/v1/chat/stream",
                headers=auth_headers,
                json={"prompt": "a question", "workspace_id": workspace_id},
            )

        body = client.get(
            f"/api/v1/analytics?workspace_id={workspace_id}", headers=auth_headers
        ).json()
        assert body["questionsAskedTotal"] >= 1

    def test_analytics_are_scoped_to_the_workspace(self, client, auth_headers):
        from tests.conftest import STRONG_PASSWORD

        signup = client.post(
            "/api/v1/auth/signup",
            json={
                "name": "Other",
                "email": "analytics_isolation@example.com",
                "password": STRONG_PASSWORD,
            },
        )
        if signup.status_code != 200:
            signup = client.post(
                "/api/v1/auth/login",
                json={"email": "analytics_isolation@example.com", "password": STRONG_PASSWORD},
            )
        headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

        body = client.get("/api/v1/analytics", headers=headers).json()
        assert body["questionsAskedTotal"] == 0
        assert body["documentsIndexedTotal"] == 0


# ---------------------------------------------------------- password reset --
class TestPasswordReset:
    pytestmark = pytest.mark.integration

    def test_requesting_a_reset_never_reveals_whether_the_account_exists(
        self, client, registered_user
    ):
        known = client.post(
            "/api/v1/auth/password-reset/request", json={"email": registered_user["email"]}
        )
        unknown = client.post(
            "/api/v1/auth/password-reset/request", json={"email": "nobody-here@example.com"}
        )
        # Differing responses turn this endpoint into an account oracle.
        assert known.status_code == unknown.status_code
        # request_id differs per request; the message must not.
        assert known.json()["message"] == unknown.json()["message"]

    def test_a_valid_token_changes_the_password(self, client, registered_user):
        from app.core.database import SessionLocal
        from app.core.password_reset import generate_reset_token
        from app.models.user import User

        session = SessionLocal()
        try:
            user = session.query(User).filter(User.email == registered_user["email"]).first()
            token, _ = generate_reset_token(session, user.id)
        finally:
            session.close()

        new_password = "Zq4%tRn8&xKp1"
        response = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": new_password},
        )
        assert response.status_code == 200

        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": registered_user["email"], "password": new_password},
            ).status_code
            == 200
        )
        # The old password must stop working.
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": registered_user["email"], "password": registered_user["password"]},
            ).status_code
            == 401
        )

    def test_a_token_cannot_be_reused(self, client, registered_user):
        from app.core.database import SessionLocal
        from app.core.password_reset import generate_reset_token
        from app.models.user import User

        session = SessionLocal()
        try:
            user = session.query(User).filter(User.email == registered_user["email"]).first()
            token, _ = generate_reset_token(session, user.id)
        finally:
            session.close()

        first = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "Zq4%tRn8&xKp1"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "Wm7!bYc3@dFh9"},
        )
        # A replayable reset token is a standing account takeover.
        assert second.status_code == 400

    def test_an_unknown_token_is_rejected(self, client):
        response = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": "not-a-real-token", "new_password": "Zq4%tRn8&xKp1"},
        )
        assert response.status_code == 400

    def test_an_expired_token_is_rejected(self, client, registered_user):
        from datetime import datetime, timedelta

        from app.core.database import SessionLocal
        from app.core.password_reset import generate_reset_token
        from app.models.password_reset import PasswordResetToken
        from app.models.user import User

        session = SessionLocal()
        try:
            user = session.query(User).filter(User.email == registered_user["email"]).first()
            token, _ = generate_reset_token(session, user.id)
            record = (
                session.query(PasswordResetToken)
                .filter(PasswordResetToken.user_id == user.id)
                .order_by(PasswordResetToken.created_at.desc())
                .first()
            )
            record.expires_at = datetime.utcnow() - timedelta(hours=1)
            session.commit()
        finally:
            session.close()

        response = client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "new_password": "Zq4%tRn8&xKp1"},
        )
        assert response.status_code == 400

    def test_a_weak_new_password_is_refused(self, client, registered_user):
        from app.core.database import SessionLocal
        from app.core.password_reset import generate_reset_token
        from app.models.user import User

        session = SessionLocal()
        try:
            user = session.query(User).filter(User.email == registered_user["email"]).first()
            token, _ = generate_reset_token(session, user.id)
        finally:
            session.close()

        response = client.post(
            "/api/v1/auth/password-reset/confirm", json={"token": token, "new_password": "password"}
        )
        # The reset path must enforce the same policy as signup.
        assert response.status_code in (400, 422)

    def test_the_stored_token_is_hashed(self, client, registered_user):
        from app.core.database import SessionLocal
        from app.core.password_reset import generate_reset_token
        from app.models.password_reset import PasswordResetToken
        from app.models.user import User

        session = SessionLocal()
        try:
            user = session.query(User).filter(User.email == registered_user["email"]).first()
            token, _ = generate_reset_token(session, user.id)
            record = (
                session.query(PasswordResetToken)
                .filter(PasswordResetToken.user_id == user.id)
                .order_by(PasswordResetToken.created_at.desc())
                .first()
            )
            # Database read access must not yield usable reset tokens.
            assert record.token_hash != token
            assert token not in record.token_hash
        finally:
            session.close()
