"""
The producers: what the application announces, to whom, and in what order.

Every event here is caused the way it is in production -- through the REST
API, or through the ingestion code a worker runs -- and observed on a real
WebSocket. Nothing is published by the test itself.

Synchronisation follows test_realtime_ws: bounded reads that return as soon
as a frame arrives, absence proved with the ping/pong sentinel, teardown
awaited on the connection's handler. No sleeps.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from tests import test_realtime_ws as ws
from tests.test_chat_reports import SOURCES_FRAME, _stream_of
from tests.test_collaboration import _token_for

# Shared fixtures, registered in this module by assignment.
owner = ws.owner
joiner = ws.joiner
shared_redis = ws.shared_redis

open_socket = ws.open_socket
ready = ws.ready
receive = ws.receive
next_event = ws.next_event
assert_quiet = ws.assert_quiet
close_code = ws.close_code
connection_of = ws.connection_of
torn_down = ws.torn_down
register = ws.register

pytestmark = pytest.mark.integration


def headers_for(account) -> dict:
    return {"Authorization": f"Bearer {account['token']}"}


def upload(client, account, workspace_id):
    body = f"Realtime producer document {uuid.uuid4().hex}. ".encode() * 30
    response = client.post(
        "/api/v1/documents/upload",
        headers=headers_for(account),
        files={"file": (f"{uuid.uuid4().hex}.txt", body, "text/plain")},
        data={"workspace_id": workspace_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def events_until(socket, done) -> list[dict]:
    """Non-presence frames up to and including the one `done` accepts."""
    collected = []
    while True:
        frame = next_event(socket)
        collected.append(frame)
        if done(frame):
            return collected


def member_id_of(client, account, workspace_id, user_id) -> str:
    items = client.get(
        f"/api/v1/workspace/members?workspace_id={workspace_id}", headers=headers_for(account)
    ).json()["items"]
    return next(m["id"] for m in items if m["userId"] == user_id)


def invite(client, account, workspace_id, email=None, role="viewer") -> dict:
    response = client.post(
        "/api/v1/workspace/invite",
        headers=headers_for(account),
        json={
            "email": email or f"invitee_{uuid.uuid4().hex[:8]}@example.com",
            "role": role,
            "workspace_id": workspace_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ documents --
class TestDocumentEvents:
    def test_an_upload_is_announced_first_then_indexed_stage_by_stage(
        self, client, owner, joiner, indexing
    ):
        """What a colleague's screen sees while someone else uploads."""
        with open_socket(client, joiner["token"], owner["workspace_id"]) as watcher:
            ready(watcher)
            document = upload(client, owner, owner["workspace_id"])
            frames = events_until(
                watcher,
                lambda f: f["type"] == "document.status" and f["data"]["status"] == "indexed",
            )

        created, statuses = frames[0], frames[1:]
        # Announced before any status: a screen must know a document exists
        # before it is told how that document's indexing is going.
        assert created["type"] == "document.created"
        assert created["data"]["id"] == document["id"]
        assert created["data"]["status"] == "queued"
        assert created["data"]["uploadedBy"], "the row renders the uploader"

        assert all(f["type"] == "document.status" for f in statuses)
        assert all(f["data"]["id"] == document["id"] for f in statuses)
        assert statuses[0]["data"]["status"] == "processing"
        progress = [f["data"]["progress"] for f in statuses]
        assert progress == sorted(progress), "progress went backwards"
        assert {20, 35, 70, 90} <= set(progress), "a stage was not announced"
        assert (statuses[-1]["data"]["status"], statuses[-1]["data"]["progress"]) == (
            "indexed",
            100,
        )
        seqs = [f["seq"] for f in frames]
        assert seqs == sorted(set(seqs))

    def test_the_uploader_hears_the_same_story(self, client, owner, indexing):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            upload(client, owner, owner["workspace_id"])
            frames = events_until(socket, lambda f: f["data"].get("status") == "indexed")
        assert frames[0]["type"] == "document.created"
        assert frames[0]["actor_id"] == owner["user_id"]

    def test_a_permanent_failure_is_announced_with_its_reason(
        self, client, owner, joiner, indexing
    ):
        """The path Celery takes once retries are exhausted."""
        from app.worker.tasks import process_document

        document = upload(client, owner, owner["workspace_id"])
        with open_socket(client, joiner["token"], owner["workspace_id"]) as watcher:
            ready(watcher)
            # Called from this thread, which has no event loop -- as a worker
            # process calls it.
            process_document.on_failure(
                RuntimeError("PDF is encrypted"), "task-1", (document["id"],), {}, None
            )
            frame = next_event(watcher)

        assert frame["type"] == "document.status"
        assert frame["data"]["status"] == "failed"
        assert "PDF is encrypted" in frame["data"]["error"]

    def test_a_stalled_document_reaped_by_the_server_is_announced(
        self, client, owner, joiner, indexing
    ):
        from app.core.database import SessionLocal
        from app.models.document import Document
        from app.worker.tasks import reap_stalled_documents

        document = upload(client, owner, owner["workspace_id"])
        db = SessionLocal()
        try:
            row = db.get(Document, document["id"])
            row.status = "processing"
            row.processing_started_at = datetime.utcnow() - timedelta(hours=2)
            db.commit()
        finally:
            db.close()

        with open_socket(client, joiner["token"], owner["workspace_id"]) as watcher:
            ready(watcher)
            assert reap_stalled_documents()["reaped"] >= 1
            frames = events_until(watcher, lambda f: f["data"].get("id") == document["id"])

        assert frames[-1]["data"]["status"] == "failed"
        assert "stopped responding" in frames[-1]["data"]["error"]

    def test_deletion_is_announced(self, client, owner, joiner, indexing):
        document = upload(client, owner, owner["workspace_id"])
        with open_socket(client, joiner["token"], owner["workspace_id"]) as watcher:
            ready(watcher)
            response = client.delete(
                f"/api/v1/documents/{document['id']}", headers=headers_for(owner)
            )
            assert response.status_code == 200
            frame = next_event(watcher)

        assert frame["type"] == "document.deleted"
        assert frame["actor_id"] == owner["user_id"]
        assert frame["data"] == {"id": document["id"]}

    def test_another_workspace_hears_nothing(self, client, owner, indexing):
        outsider = register(client, "doc_outsider")
        with open_socket(client, outsider["token"], outsider["workspace_id"]) as theirs:
            ready(theirs)
            upload(client, owner, owner["workspace_id"])
            assert_quiet(theirs)

    def test_the_worker_reaches_the_browser_through_redis(self, client, owner, shared_redis):
        """_set_state, called from a thread with no event loop, as in Celery.

        The document row is created directly: an upload here would find a
        broker configured and try to reach a real Celery broker at the fake
        address, which is not what this test is about.
        """
        from app.core.database import SessionLocal
        from app.models.document import Document
        from app.worker.tasks import _set_state

        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            baseline = ready(socket)["data"]

            db = SessionLocal()
            try:
                row = Document(
                    workspace_id=owner["workspace_id"],
                    filename="worker.txt",
                    original_filename="worker.txt",
                    file_type="txt",
                    file_size=10,
                    status="queued",
                )
                db.add(row)
                db.commit()
                _set_state(db, row, status="processing", progress=42)
                document_id = row.id
            finally:
                db.close()

            frame = next_event(socket)
        assert frame["data"] == {**frame["data"], "id": document_id, "progress": 42}
        # Sequenced in Redis alongside the API's own events.
        assert frame["epoch"] == baseline["epoch"]
        assert frame["seq"] > baseline["seq"]


# -------------------------------------------------------------- collaboration --
class TestCollaborationEvents:
    def test_an_invitation_reaches_only_those_who_may_invite_and_says_only_its_id(
        self, client, owner, joiner
    ):
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as admin,
            open_socket(client, joiner["token"], owner["workspace_id"]) as editor,
        ):
            ready(admin)
            ready(editor)
            invitation = invite(client, owner, owner["workspace_id"])

            frame = next_event(admin)
            assert frame["type"] == "invitation.sent"
            # Not the email, not the role, never the token: the list is
            # refetched, with authorization applied at that moment.
            assert frame["data"] == {"id": invitation["id"]}
            assert_quiet(editor)

    def test_revocation_is_announced_to_managers_only(self, client, owner, joiner):
        invitation = invite(client, owner, owner["workspace_id"])
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as admin,
            open_socket(client, joiner["token"], owner["workspace_id"]) as editor,
        ):
            ready(admin)
            ready(editor)
            revoked = client.post(
                "/api/v1/workspace/invite/revoke",
                headers=headers_for(owner),
                json={"invitation_id": invitation["id"], "workspace_id": owner["workspace_id"]},
            )
            assert revoked.status_code == 200, revoked.text

            frame = next_event(admin)
            assert frame["type"] == "invitation.revoked"
            assert frame["data"] == {"id": invitation["id"]}
            assert_quiet(editor)

    def test_acceptance_tells_managers_and_adds_a_member_for_everyone(self, client, owner, joiner):
        invitee = register(client, "ws_invitee")
        invitation = invite(client, owner, owner["workspace_id"], email=invitee["email"])
        token = _token_for(invitation["id"])

        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as admin,
            open_socket(client, joiner["token"], owner["workspace_id"]) as editor,
        ):
            ready(admin)
            ready(editor)
            accepted = client.post(
                "/api/v1/workspace/invite/accept",
                headers=headers_for(invitee),
                json={"token": token},
            )
            assert accepted.status_code == 200, accepted.text

            assert [next_event(admin)["type"] for _ in range(2)] == [
                "invitation.accepted",
                "member.added",
            ]
            added = next_event(editor)
            assert added["type"] == "member.added"
            assert added["data"]["userId"] == invitee["user_id"]
            assert added["data"]["role"] == "viewer"
            assert_quiet(editor)

    def test_a_role_change_applies_to_the_open_socket(self, client, owner, joiner):
        member_id = member_id_of(client, owner, owner["workspace_id"], joiner["user_id"])
        with open_socket(client, joiner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            promoted = client.patch(
                f"/api/v1/workspace/members/{member_id}",
                headers=headers_for(owner),
                json={"role": "admin", "workspace_id": owner["workspace_id"]},
            )
            assert promoted.status_code == 200, promoted.text
            changed = next_event(socket)
            assert changed["type"] == "member.role_changed"
            assert changed["data"]["role"] == "admin"

            # Now an admin, the same socket hears invitations.
            invite(client, owner, owner["workspace_id"])
            assert next_event(socket)["type"] == "invitation.sent"

    def test_removal_is_announced_and_closes_the_removed_members_socket(
        self, client, owner, joiner
    ):
        member_id = member_id_of(client, owner, owner["workspace_id"], joiner["user_id"])
        with open_socket(client, owner["token"], owner["workspace_id"]) as admin:
            ready(admin)
            with open_socket(client, joiner["token"], owner["workspace_id"]) as removed:
                connection = connection_of(client, ready(removed))
                assert receive(admin)["type"] == "presence.online"

                response = client.delete(
                    f"/api/v1/workspace/members/{member_id}?workspace_id={owner['workspace_id']}",
                    headers=headers_for(owner),
                )
                assert response.status_code == 200, response.text
                assert close_code(removed) == ws.CLOSE_FORBIDDEN

            assert torn_down(client, connection)
            gone = next_event(admin)
            assert gone["type"] == "member.removed"
            assert gone["data"]["userId"] == joiner["user_id"]

        # And they cannot come back in.
        assert ws.rejected(client, joiner["token"], owner["workspace_id"]) == ws.CLOSE_FORBIDDEN


class TestOwnershipAndResend:
    def test_an_ownership_transfer_announces_both_role_changes(self, client, owner, joiner):
        member_id = member_id_of(client, owner, owner["workspace_id"], joiner["user_id"])
        with open_socket(client, joiner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            response = client.post(
                f"/api/v1/workspace/transfer-ownership?member_id={member_id}",
                headers=headers_for(owner),
                json={"role": "owner", "workspace_id": owner["workspace_id"]},
            )
            assert response.status_code == 200, response.text

            changes = {
                (f["data"]["userId"], f["data"]["role"])
                for f in (next_event(socket) for _ in range(2))
            }
            assert changes == {(owner["user_id"], "admin"), (joiner["user_id"], "owner")}

    def test_a_resend_revokes_the_old_invitation_and_announces_the_new(self, client, owner, joiner):
        original = invite(client, owner, owner["workspace_id"])
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as admin,
            open_socket(client, joiner["token"], owner["workspace_id"]) as editor,
        ):
            ready(admin)
            ready(editor)
            resent = client.post(
                "/api/v1/workspace/invite/resend",
                headers=headers_for(owner),
                json={"invitation_id": original["id"], "workspace_id": owner["workspace_id"]},
            )
            assert resent.status_code == 200, resent.text
            replacement = resent.json()["id"]
            assert replacement != original["id"]

            revoked, sent = next_event(admin), next_event(admin)
            assert (revoked["type"], revoked["data"]) == (
                "invitation.revoked",
                {"id": original["id"]},
            )
            assert (sent["type"], sent["data"]) == ("invitation.sent", {"id": replacement})
            assert_quiet(editor)


# ----------------------------------------------------------------------- chat --
def create_session(client, account, workspace_id) -> dict:
    response = client.post(
        f"/api/v1/chat/sessions?workspace_id={workspace_id}", headers=headers_for(account)
    )
    assert response.status_code == 200, response.text
    return response.json()


def stream(client, account, workspace_id, prompt, session_id=None):
    payload = {"prompt": prompt, "workspace_id": workspace_id}
    if session_id:
        payload["session_id"] = session_id
    with patch("app.api.v1.chat.stream_rag_response", _stream_of("An answer.", SOURCES_FRAME)):
        response = client.post("/api/v1/chat/stream", headers=headers_for(account), json=payload)
    assert response.status_code == 200, response.text
    return response


class TestChatEvents:
    """Chat sessions are private: every event goes to the owner's tabs alone."""

    def test_a_new_session_reaches_only_its_owner(self, client, owner, joiner):
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as mine,
            open_socket(client, joiner["token"], owner["workspace_id"]) as colleague,
        ):
            ready(mine)
            ready(colleague)
            session = create_session(client, owner, owner["workspace_id"])

            frame = next_event(mine)
            assert frame["type"] == "chat.created"
            assert frame["data"] == {"id": session["id"], "title": session["title"]}
            assert_quiet(colleague)

    def test_a_streamed_turn_renames_the_session_and_adds_both_messages(
        self, client, owner, joiner
    ):
        session = create_session(client, owner, owner["workspace_id"])
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as mine,
            open_socket(client, joiner["token"], owner["workspace_id"]) as colleague,
        ):
            ready(mine)
            ready(colleague)
            stream(client, owner, owner["workspace_id"], "My private question", session["id"])

            renamed, asked, answered = (next_event(mine) for _ in range(3))
            assert renamed["type"] == "chat.renamed"
            assert renamed["data"]["title"] == "My private question"
            assert (asked["type"], asked["data"]["role"]) == ("chat.message", "user")
            assert (answered["type"], answered["data"]["role"]) == ("chat.message", "assistant")
            assert asked["data"]["sessionId"] == answered["data"]["sessionId"] == session["id"]
            # The title is the opening words of a private prompt.
            assert_quiet(colleague)

    def test_a_session_created_by_the_first_message_is_announced(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as mine:
            ready(mine)
            stream(client, owner, owner["workspace_id"], "Opening question")
            created = next_event(mine)
            assert created["type"] == "chat.created"
            assert created["data"]["title"] == "Opening question"

    def test_deleting_a_session_is_announced_and_removes_its_messages(self, client, owner, joiner):
        from app.core.database import SessionLocal
        from app.models.chat import ChatMessage, ChatSession

        session = create_session(client, owner, owner["workspace_id"])
        stream(client, owner, owner["workspace_id"], "Something to delete", session["id"])

        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as mine,
            open_socket(client, joiner["token"], owner["workspace_id"]) as colleague,
        ):
            ready(mine)
            ready(colleague)
            response = client.delete(
                f"/api/v1/chat/sessions/{session['id']}", headers=headers_for(owner)
            )
            assert response.status_code == 200, response.text
            frame = next_event(mine)
            assert frame["type"] == "chat.deleted"
            assert frame["data"] == {"id": session["id"]}
            assert_quiet(colleague)

        db = SessionLocal()
        try:
            assert db.get(ChatSession, session["id"]) is None
            remaining = (
                db.query(ChatMessage).filter(ChatMessage.session_id == session["id"]).count()
            )
            assert remaining == 0, "messages must go with their session"
        finally:
            db.close()

    def test_another_members_session_cannot_be_deleted(self, client, owner, joiner):
        """Same workspace, someone else's conversation: reported as missing."""
        session = create_session(client, owner, owner["workspace_id"])
        with open_socket(client, owner["token"], owner["workspace_id"]) as mine:
            ready(mine)
            response = client.delete(
                f"/api/v1/chat/sessions/{session['id']}", headers=headers_for(joiner)
            )
            assert response.status_code == 404
            assert_quiet(mine)

        listed = client.get(
            f"/api/v1/chat/sessions?workspace_id={owner['workspace_id']}",
            headers=headers_for(owner),
        ).json()["items"]
        assert session["id"] in [s["id"] for s in listed]

    def test_deletion_requires_authentication(self, client, owner):
        session = create_session(client, owner, owner["workspace_id"])
        assert client.delete(f"/api/v1/chat/sessions/{session['id']}").status_code == 401

    def test_an_unknown_session_is_not_found(self, client, owner):
        response = client.delete(
            f"/api/v1/chat/sessions/{uuid.uuid4()}", headers=headers_for(owner)
        )
        assert response.status_code == 404
