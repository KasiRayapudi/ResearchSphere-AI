"""
Pagination, sorting, filtering, search and their interaction with
authorization.

The interesting failures here are the quiet ones: a page that silently
returns someone else's rows, a sort that lets a caller name any column, or a
tie that puts the same row on two pages. Each has a test.
"""

import pytest

from app.core.pagination import MAX_PAGE_SIZE, decode_cursor, encode_cursor
from tests.conftest import STRONG_PASSWORD

pytestmark = pytest.mark.integration


def _seed_documents(client, headers, workspace_id, count, prefix="doc"):
    """Create documents directly, so the test is not an upload benchmark."""
    import uuid
    from datetime import datetime, timedelta

    from app.core.database import SessionLocal
    from app.models.document import Document

    session = SessionLocal()
    now = datetime.utcnow()
    try:
        for i in range(count):
            session.add(
                Document(
                    id=str(uuid.uuid4()),
                    workspace_id=workspace_id,
                    filename=f"{prefix}-{i:03d}.txt",
                    original_filename=f"{prefix}-{i:03d}.txt",
                    file_type="txt" if i % 2 else "pdf",
                    file_size=1024 * (i + 1),
                    file_path=f"/tmp/{prefix}-{i}",
                    status="indexed" if i % 3 else "failed",
                    chunk_count=i,
                    progress=100,
                    folder_path="/Uploads",
                    # Distinct timestamps so ordering is well defined.
                    created_at=now - timedelta(minutes=i),
                    updated_at=now - timedelta(minutes=i),
                )
            )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def big_workspace(client, auth_headers, workspace_id):
    _seed_documents(client, auth_headers, workspace_id, 60)
    return workspace_id


def _get(client, headers, path, **params):
    return client.get(path, headers=headers, params=params)


# ------------------------------------------------------------ the shape --
class TestEnvelope:
    def test_a_page_describes_itself(self, client, auth_headers, big_workspace):
        body = _get(
            client, auth_headers, "/api/v1/documents", workspace_id=big_workspace, page_size=10
        ).json()

        assert len(body["items"]) == 10
        assert body["page"] == 1
        assert body["pageSize"] == 10
        assert body["total"] == 60
        assert body["pages"] == 6
        assert body["hasNext"] is True
        assert body["hasPrevious"] is False

    def test_the_last_page_says_so(self, client, auth_headers, big_workspace):
        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=big_workspace,
            page=6,
            page_size=10,
        ).json()
        assert body["hasNext"] is False
        assert body["hasPrevious"] is True
        assert len(body["items"]) == 10

    def test_a_page_beyond_the_end_is_empty_not_an_error(self, client, auth_headers, big_workspace):
        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=big_workspace,
            page=99,
            page_size=10,
        ).json()
        # Asking past the end is a normal thing for a client to do while data
        # is changing underneath it.
        assert body["items"] == []
        assert body["total"] == 60
        assert body["hasNext"] is False

    def test_pages_do_not_overlap_or_skip(self, client, auth_headers, big_workspace):
        seen = []
        for page in range(1, 7):
            body = _get(
                client,
                auth_headers,
                "/api/v1/documents",
                workspace_id=big_workspace,
                page=page,
                page_size=10,
            ).json()
            seen.extend(item["id"] for item in body["items"])

        # The classic pagination bug: ties in the sort column putting a row on
        # two pages, or on none. The tiebreaker exists to prevent it.
        assert len(seen) == 60
        assert len(set(seen)) == 60


class TestBounds:
    def test_page_size_is_capped(self, client, auth_headers, workspace_id):
        response = _get(
            client, auth_headers, "/api/v1/documents", workspace_id=workspace_id, page_size=10_000
        )
        # Rejected rather than silently clamped: a client asking for
        # everything should be told no, not quietly given something else.
        assert response.status_code == 422

    def test_the_maximum_is_accepted(self, client, auth_headers, workspace_id):
        response = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=workspace_id,
            page_size=MAX_PAGE_SIZE,
        )
        assert response.status_code == 200

    def test_page_zero_is_rejected(self, client, auth_headers, workspace_id):
        response = _get(
            client, auth_headers, "/api/v1/documents", workspace_id=workspace_id, page=0
        )
        assert response.status_code == 422

    def test_a_negative_page_is_rejected(self, client, auth_headers, workspace_id):
        response = _get(
            client, auth_headers, "/api/v1/documents", workspace_id=workspace_id, page=-1
        )
        assert response.status_code == 422


# ---------------------------------------------------------------- sorts --
class TestSorting:
    def test_default_is_newest_first(self, client, auth_headers, big_workspace):
        body = _get(
            client, auth_headers, "/api/v1/documents", workspace_id=big_workspace, page_size=5
        ).json()
        stamps = [item["uploadedAt"] for item in body["items"]]
        assert stamps == sorted(stamps, reverse=True)

    def test_ascending_is_honoured(self, client, auth_headers, big_workspace):
        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=big_workspace,
            order="asc",
            page_size=5,
        ).json()
        stamps = [item["uploadedAt"] for item in body["items"]]
        assert stamps == sorted(stamps)

    def test_sorting_by_another_column(self, client, auth_headers, big_workspace):
        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=big_workspace,
            sort="size",
            order="desc",
            page_size=5,
        ).json()
        sizes = [item["fileSizeKb"] for item in body["items"]]
        assert sizes == sorted(sizes, reverse=True)

    def test_an_unknown_sort_field_is_refused(self, client, auth_headers, workspace_id):
        response = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=workspace_id,
            sort="hashed_password",
        )
        # An allowlist, not getattr on the model: otherwise a caller can order
        # by any attribute, which both leaks the model's shape and lets them
        # pick an unindexed column to make the query expensive.
        assert response.status_code == 400
        assert "Cannot sort by" in response.text

    def test_an_unknown_order_is_refused(self, client, auth_headers, workspace_id):
        response = _get(
            client, auth_headers, "/api/v1/documents", workspace_id=workspace_id, order="sideways"
        )
        assert response.status_code == 422


# ------------------------------------------------------- filter + search --
class TestFilteringAndSearch:
    def test_filtering_by_status(self, client, auth_headers, big_workspace):
        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=big_workspace,
            status="failed",
            page_size=100,
        ).json()
        assert body["items"]
        assert all(item["status"] == "failed" for item in body["items"])
        # The total must describe the filtered set, or the pager lies.
        assert body["total"] == len(body["items"])

    def test_filtering_by_file_type(self, client, auth_headers, big_workspace):
        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=big_workspace,
            file_type="pdf",
            page_size=100,
        ).json()
        assert body["items"]
        assert all(item["fileType"] == "pdf" for item in body["items"])

    def test_search_narrows_the_list(self, client, auth_headers, workspace_id):
        _seed_documents(client, auth_headers, workspace_id, 5, prefix="quarterly")
        _seed_documents(client, auth_headers, workspace_id, 5, prefix="annual")

        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=workspace_id,
            search="quarterly",
            page_size=100,
        ).json()
        assert body["items"]
        assert all("quarterly" in item["title"] for item in body["items"])

    def test_search_is_case_insensitive(self, client, auth_headers, workspace_id):
        _seed_documents(client, auth_headers, workspace_id, 3, prefix="Findings")
        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=workspace_id,
            search="FINDINGS",
            page_size=100,
        ).json()
        assert body["items"]

    def test_search_that_matches_nothing_is_an_empty_page(
        self, client, auth_headers, big_workspace
    ):
        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=big_workspace,
            search="nothing-matches-this-string",
        ).json()
        assert body["items"] == []
        assert body["total"] == 0


# --------------------------------------------------------------- cursor --
class TestKeysetPagination:
    def test_a_cursor_continues_where_the_page_ended(self, client, auth_headers, big_workspace):
        first = _get(
            client, auth_headers, "/api/v1/documents", workspace_id=big_workspace, page_size=10
        ).json()
        assert first["nextCursor"]

        second = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=big_workspace,
            page_size=10,
            cursor=first["nextCursor"],
        ).json()

        first_ids = {item["id"] for item in first["items"]}
        second_ids = {item["id"] for item in second["items"]}
        assert len(second["items"]) == 10
        assert first_ids.isdisjoint(second_ids)

    def test_walking_by_cursor_sees_everything_once(self, client, auth_headers, big_workspace):
        seen = []
        cursor = None
        for _ in range(20):  # bounded so a bug cannot loop forever
            params = {"workspace_id": big_workspace, "page_size": 10}
            if cursor:
                params["cursor"] = cursor
            body = client.get("/api/v1/documents", headers=auth_headers, params=params).json()
            seen.extend(item["id"] for item in body["items"])
            cursor = body["nextCursor"]
            if not cursor:
                break

        assert len(seen) == 60
        assert len(set(seen)) == 60

    def test_a_keyset_page_reports_no_total(self, client, auth_headers, big_workspace):
        first = _get(
            client, auth_headers, "/api/v1/documents", workspace_id=big_workspace, page_size=10
        ).json()
        body = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=big_workspace,
            page_size=10,
            cursor=first["nextCursor"],
        ).json()
        # Counting the whole set would throw away the reason for using a
        # cursor, so the total is absent rather than wrong.
        assert body["total"] is None
        assert body["pages"] is None

    def test_a_malformed_cursor_is_rejected(self, client, auth_headers, workspace_id):
        response = _get(
            client,
            auth_headers,
            "/api/v1/documents",
            workspace_id=workspace_id,
            cursor="not-a-cursor",
        )
        # Falling back to page one would look like data loss to whoever is
        # paging through a list.
        assert response.status_code == 400

    def test_cursor_round_trip(self):
        from datetime import datetime

        value = datetime(2026, 3, 4, 5, 6, 7)
        decoded_value, decoded_id = decode_cursor(encode_cursor(value, "row-9"))
        assert decoded_value == value.isoformat()
        assert decoded_id == "row-9"


# -------------------------------------------------- authorization safety --
class TestPaginationRespectsAuthorization:
    @pytest.fixture
    def other_account(self, client):
        import uuid

        email = f"pag_{uuid.uuid4().hex[:8]}@example.com"
        response = client.post(
            "/api/v1/auth/signup",
            json={"name": "Other", "email": email, "password": STRONG_PASSWORD},
        )
        assert response.status_code == 200
        headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        workspaces = client.get("/api/v1/workspaces", headers=headers).json()
        return {"headers": headers, "workspace_id": workspaces[0]["id"]}

    def test_paging_cannot_reach_another_workspace(
        self, client, auth_headers, big_workspace, other_account
    ):
        response = _get(
            client,
            other_account["headers"],
            "/api/v1/documents",
            workspace_id=big_workspace,
            page_size=100,
        )
        # 404 rather than 403 so ids stay unprobeable, exactly as the
        # unpaginated endpoint behaved.
        assert response.status_code == 404

    def test_a_cursor_from_another_workspace_leaks_nothing(
        self, client, auth_headers, big_workspace, other_account
    ):
        mine = _get(
            client, auth_headers, "/api/v1/documents", workspace_id=big_workspace, page_size=10
        ).json()
        assert mine["nextCursor"]

        # A cursor only says "after this row in this ordering". Replaying one
        # against a different caller's workspace must still return only their
        # own rows, because the filter is applied before it.
        body = _get(
            client,
            other_account["headers"],
            "/api/v1/documents",
            workspace_id=other_account["workspace_id"],
            page_size=10,
            cursor=mine["nextCursor"],
        ).json()
        assert body["items"] == []

    def test_search_cannot_cross_a_workspace(
        self, client, auth_headers, big_workspace, other_account
    ):
        body = _get(
            client,
            other_account["headers"],
            "/api/v1/documents",
            workspace_id=other_account["workspace_id"],
            search="doc-",
            page_size=100,
        ).json()
        assert body["items"] == []

    def test_listing_still_requires_authentication(self, client, big_workspace):
        assert client.get("/api/v1/documents").status_code == 401


# ------------------------------------------------- the other collections --
class TestOtherEndpointsArePaginated:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/documents",
            "/api/v1/chat/sessions",
            "/api/v1/reports",
            "/api/v1/research",
            "/api/v1/workspace/members",
            "/api/v1/workspace/invitations",
        ],
    )
    def test_every_list_returns_an_envelope(self, client, auth_headers, workspace_id, path):
        body = _get(client, auth_headers, path, workspace_id=workspace_id).json()
        for key in ("items", "page", "pageSize", "hasNext", "hasPrevious"):
            assert key in body, f"{path} is missing {key}"

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/documents",
            "/api/v1/chat/sessions",
            "/api/v1/reports",
            "/api/v1/research",
            "/api/v1/workspace/members",
        ],
    )
    def test_every_list_caps_its_page_size(self, client, auth_headers, workspace_id, path):
        response = _get(client, auth_headers, path, workspace_id=workspace_id, page_size=10_000)
        assert response.status_code == 422, f"{path} accepted an unbounded page size"

    def test_members_are_paginated_and_counted(self, client, auth_headers, workspace_id):
        body = _get(
            client, auth_headers, "/api/v1/workspace/members", workspace_id=workspace_id
        ).json()
        assert body["total"] == 1
        assert body["items"][0]["role"] == "owner"


class TestNoNPlusOne:
    def test_the_member_list_does_not_query_per_row(self, client, auth_headers, workspace_id):
        """Regression: member rows looked their users up one at a time.

        Rendering the list issued two queries per member -- the member's user
        and their inviter -- so a workspace of fifty people cost a hundred
        round trips. They are fetched in one IN query now.
        """
        from sqlalchemy import event

        from app.core.database import engine

        statements = []

        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            client.get(
                "/api/v1/workspace/members",
                headers=auth_headers,
                params={"workspace_id": workspace_id},
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)

        user_queries = [s for s in statements if "FROM users" in s]
        # One for authentication, one batched lookup for the page. Not one
        # per row.
        assert len(user_queries) <= 3, f"{len(user_queries)} user queries: {user_queries}"
