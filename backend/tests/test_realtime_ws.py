"""
The WebSocket endpoint, end to end.

Every test drives a real connection through Starlette's WebSocket test client
against the real application: a real handshake, real subprotocol negotiation,
real close codes. Nothing about the server is faked.

Synchronisation
---------------

Two properties of the test client shape the helpers below.

* The application runs on the TestClient's portal loop, in another thread.
  Events are therefore published *on that loop* through ``client.portal`` --
  touching the manager's asyncio queues from the test thread would not be
  thread-safe, and a writer woken from the wrong thread may not run promptly.
* Leaving ``with websocket_connect(...)`` does not wait for the server to
  finish. Starlette 0.38 schedules the app on the portal and never joins that
  task, so ``__exit__`` returns while the server's teardown is still queued.
  Tests that assert on teardown wait for the connection's own handler task,
  which the manager exposes -- not for time to pass.

Reads are bounded so a missing event fails instead of hanging. The bound is a
failure limit, not a delay: every read returns the moment its frame arrives.
Absence is proved with the ping/pong sentinel: frames to one socket are
strictly ordered, so a pong arriving next means nothing was queued ahead.
"""

import asyncio
import json
import queue
import uuid
from datetime import timedelta

import fakeredis
import fakeredis.aioredis
import pytest
from starlette.websockets import WebSocketDisconnect

from app.core import redis_client
from app.core.config import settings
from app.realtime.broker import EventBroker, get_broker, publish_from_worker
from app.realtime.events import PROTOCOL_VERSION, EventType, make_event
from app.realtime.manager import (
    CLOSE_FORBIDDEN,
    CLOSE_TIMEOUT,
    CLOSE_TOO_MANY,
    CLOSE_UNAUTHORIZED,
    ConnectionManager,
    get_manager,
)

WS_PATH = "/api/v1/ws"
#: A failure bound, never a synchronisation mechanism.
FAIL_AFTER = 5.0
PRESENCE = (EventType.PRESENCE_ONLINE, EventType.PRESENCE_OFFLINE)


# ------------------------------------------------------------------ helpers --
def open_socket(client, token: str, workspace_id: str, headers: dict | None = None):
    """Connect the way a browser does: the token rides in the subprotocol."""
    extra = {"headers": dict(headers)} if headers else {}
    return client.websocket_connect(
        f"{WS_PATH}?workspace_id={workspace_id}", subprotocols=["bearer", token], **extra
    )


def receive(socket, timeout: float = FAIL_AFTER) -> dict:
    """The next frame, or a clear failure -- never an indefinite hang.

    Mirrors WebSocketTestSession.receive_json, which blocks without a bound.
    Coupled to Starlette 0.38's session internals on purpose: a hung suite
    hides which assertion failed.
    """
    try:
        message = socket._send_queue.get(timeout=timeout)
    except queue.Empty:
        raise AssertionError(f"no frame arrived within {timeout}s") from None
    if isinstance(message, BaseException):
        raise message
    socket._raise_on_close(message)
    return json.loads(message["text"])


def on_loop(client, fn, *args):
    """Run on the application's event loop, where the manager's state lives."""
    return client.portal.call(fn, *args)


def emit(client, event_type: str, workspace_id: str, data=None, actor_id=None, **audience):
    """Publish through the process broker, as an API handler would."""
    event = make_event(event_type, workspace_id, data or {}, actor_id, **audience)
    return on_loop(client, get_broker().publish, event)


def ready(socket, *, arrival: bool = True) -> dict:
    """Consume the ready frame, and the user's own arrival if there is one.

    A user's first connection in a workspace is announced to the room, which
    includes that connection itself. A second tab is not announced at all.
    """
    frame = receive(socket)
    assert frame["type"] == EventType.CONNECTED, f"expected ready frame, got {frame['type']}"
    if arrival:
        own = receive(socket)
        assert own["type"] == EventType.PRESENCE_ONLINE, own
        assert own["data"]["userId"] == frame["data"]["userId"]
    return frame


def assert_quiet(socket) -> None:
    """Prove nothing is queued for this socket."""
    marker = uuid.uuid4().hex
    socket.send_json({"type": "ping", "t": marker})
    frame = receive(socket)
    assert (
        frame["type"] == EventType.PONG and frame["data"]["t"] == marker
    ), f"a frame was queued that should not have been: {frame}"


def next_event(socket) -> dict:
    """The next frame that is not presence."""
    while True:
        frame = receive(socket)
        if frame["type"] not in PRESENCE:
            return frame


def connection_of(client, ready_frame: dict):
    connection = on_loop(client, get_manager().get, ready_frame["data"]["connectionId"])
    assert connection is not None, "the server has no record of this connection"
    return connection


def torn_down(client, connection) -> bool:
    """Wait for the server to finish tearing a connection down."""
    return on_loop(client, get_manager().wait_for_teardown, connection)


def close_code(socket) -> int:
    """Read until the server closes the socket; return the code it used."""
    for _ in range(50):
        try:
            receive(socket)
        except WebSocketDisconnect as closed:
            return closed.code
    raise AssertionError("the server never closed the socket")


def rejected(client, token: str, workspace_id: str, headers: dict | None = None) -> int:
    """Close code for a connection the server refuses before accepting."""
    with pytest.raises(WebSocketDisconnect) as closed:
        with open_socket(client, token, workspace_id, headers) as socket:
            receive(socket)
    return closed.value.code


def replay_after(socket, after: int, epoch: str) -> tuple[list[dict], dict]:
    """Ask for a resume; return the replayed frames and the resumed report."""
    socket.send_json({"type": "resume", "after": after, "epoch": epoch})
    frames = []
    while True:
        frame = receive(socket)
        if frame["type"] == EventType.RESUMED:
            return frames, frame["data"]
        frames.append(frame)


def position(client, workspace_id: str) -> tuple[int, str]:
    return on_loop(client, get_broker().current_position, workspace_id)


def register(client, prefix: str) -> dict:
    """A fresh account.

    Not the admin_user fixture: it shares the function-scoped unique_email
    with registered_user, so asking for both in one test makes the second
    signup collide on an address that already exists.
    """
    from tests.conftest import STRONG_PASSWORD

    email = f"{prefix}_{uuid.uuid4().hex[:10]}@example.com"
    response = client.post(
        "/api/v1/auth/signup",
        json={"name": "WS Test User", "email": email, "password": STRONG_PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    headers = {"Authorization": f"Bearer {payload['access_token']}"}
    workspaces = client.get("/api/v1/workspaces", headers=headers).json()
    return {
        "token": payload["access_token"],
        "headers": headers,
        "user_id": payload["user"]["id"],
        "email": email,
        "workspace_id": workspaces[0]["id"],
    }


@pytest.fixture
def owner(registered_user, workspace_id):
    return {
        "token": registered_user["access_token"],
        "user_id": registered_user["user"]["id"],
        "workspace_id": workspace_id,
    }


@pytest.fixture
def joiner(client, workspace_id):
    """A second account, made an editor of the owner's workspace."""
    from app.core.database import SessionLocal
    from app.models.membership import WorkspaceMember

    account = register(client, "ws_joiner")
    db = SessionLocal()
    try:
        db.add(
            WorkspaceMember(workspace_id=workspace_id, user_id=account["user_id"], role="editor")
        )
        db.commit()
    finally:
        db.close()
    return account


@pytest.fixture
def outsider(client):
    """An account with a workspace of its own and no access to the owner's."""
    return register(client, "ws_outsider")


# ------------------------------------------------------------ authentication --
class TestAuthentication:
    def test_a_valid_token_connects(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            frame = ready(socket)
            assert frame["data"]["userId"] == owner["user_id"]
            assert frame["data"]["protocol"] == PROTOCOL_VERSION

    def test_the_subprotocol_is_echoed(self, client, owner):
        """A browser closes the socket if the server does not select one."""
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            assert socket.accepted_subprotocol == "bearer"

    def test_no_token_is_rejected(self, client, workspace_id):
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(f"{WS_PATH}?workspace_id={workspace_id}") as socket:
                receive(socket)
        assert closed.value.code == CLOSE_UNAUTHORIZED

    def test_a_garbage_token_is_rejected(self, client, workspace_id):
        assert rejected(client, "not-a-jwt", workspace_id) == CLOSE_UNAUTHORIZED

    def test_a_token_signed_by_someone_else_is_rejected(self, client, workspace_id):
        from jose import jwt

        forged = jwt.encode(
            {"sub": "whoever", "exp": 9999999999, "iat": 1, "type": "access"},
            "an-attacker-key-that-is-not-ours-at-all",
            algorithm="HS256",
        )
        assert rejected(client, forged, workspace_id) == CLOSE_UNAUTHORIZED

    def test_an_expired_token_is_rejected(self, client, owner):
        from app.core.security import create_access_token

        expired = create_access_token({"sub": owner["user_id"]}, timedelta(minutes=-30))
        assert rejected(client, expired, owner["workspace_id"]) == CLOSE_UNAUTHORIZED

    def test_a_refresh_token_cannot_be_used(self, client, registered_user, workspace_id):
        """Token type is enforced, as it is on the REST API."""
        refresh = registered_user.get("refresh_token")
        assert refresh, "fixture should provide a refresh token"
        assert rejected(client, refresh, workspace_id) == CLOSE_UNAUTHORIZED

    def test_a_token_in_the_url_is_ignored(self, client, owner):
        """A query-string token would be written to every access log."""
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(
                f"{WS_PATH}?workspace_id={owner['workspace_id']}&token={owner['token']}"
            ) as socket:
                receive(socket)
        assert closed.value.code == CLOSE_UNAUTHORIZED

    def test_a_missing_workspace_is_rejected(self, client, owner):
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect(WS_PATH, subprotocols=["bearer", owner["token"]]) as s:
                receive(s)
        assert closed.value.code == CLOSE_FORBIDDEN

    def test_the_endpoint_can_be_switched_off(self, client, owner, monkeypatch):
        monkeypatch.setattr(settings, "WS_ENABLED", False)
        assert rejected(client, owner["token"], owner["workspace_id"]) == CLOSE_FORBIDDEN

    def test_a_deactivated_account_cannot_connect(self, client, owner):
        from app.core.database import SessionLocal
        from app.models.user import User

        def set_active(value: bool) -> None:
            db = SessionLocal()
            try:
                db.query(User).filter(User.id == owner["user_id"]).update({"is_active": value})
                db.commit()
            finally:
                db.close()

        set_active(False)
        try:
            assert rejected(client, owner["token"], owner["workspace_id"]) == CLOSE_UNAUTHORIZED
        finally:
            set_active(True)


class TestOrigin:
    """Browsers do not apply CORS to WebSockets; the server has to check."""

    def test_a_foreign_origin_is_rejected(self, client, owner):
        code = rejected(
            client, owner["token"], owner["workspace_id"], {"origin": "https://evil.example"}
        )
        assert code == CLOSE_FORBIDDEN

    def test_an_allowed_origin_connects(self, client, owner):
        origin = settings.cors_origins[0]
        with open_socket(client, owner["token"], owner["workspace_id"], {"origin": origin}) as s:
            ready(s)

    def test_a_trailing_slash_does_not_matter(self, client, owner):
        origin = settings.cors_origins[0].rstrip("/") + "/"
        with open_socket(client, owner["token"], owner["workspace_id"], {"origin": origin}) as s:
            ready(s)

    def test_the_scheme_must_match(self, client, owner):
        allowed = settings.cors_origins[0]
        swapped = (
            allowed.replace("http://", "https://", 1)
            if allowed.startswith("http://")
            else allowed.replace("https://", "http://", 1)
        )
        code = rejected(client, owner["token"], owner["workspace_id"], {"origin": swapped})
        assert code == CLOSE_FORBIDDEN


# ----------------------------------------------------------------- isolation --
class TestWorkspaceIsolation:
    def test_a_non_member_cannot_subscribe(self, client, outsider, workspace_id):
        """A valid token is not authority over somebody else's workspace."""
        assert rejected(client, outsider["token"], workspace_id) == CLOSE_FORBIDDEN

    def test_events_never_cross_workspaces(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            emit(client, EventType.DOCUMENT_CREATED, "some-other-workspace", {"id": "not-yours"})
            emit(client, EventType.DOCUMENT_CREATED, owner["workspace_id"], {"id": "yours"})
            assert next_event(socket)["data"]["id"] == "yours", "an event leaked across workspaces"

    def test_two_users_in_different_workspaces_are_independent(self, client, owner, outsider):
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as mine,
            open_socket(client, outsider["token"], outsider["workspace_id"]) as theirs,
        ):
            ready(mine)
            ready(theirs)
            emit(client, EventType.CHAT_CREATED, owner["workspace_id"], {"id": "mine"})
            emit(client, EventType.CHAT_CREATED, outsider["workspace_id"], {"id": "theirs"})
            assert next_event(mine)["data"]["id"] == "mine"
            assert next_event(theirs)["data"]["id"] == "theirs"
            assert_quiet(mine)
            assert_quiet(theirs)

    def test_presence_of_another_workspace_is_not_exposed(self, client, owner, outsider):
        with open_socket(client, outsider["token"], outsider["workspace_id"]) as theirs:
            ready(theirs)
            with open_socket(client, owner["token"], owner["workspace_id"]) as mine:
                assert ready(mine)["data"]["online"] == [owner["user_id"]]
            assert_quiet(theirs)


class TestAudience:
    """Within one workspace, some events belong to one user or to some roles."""

    def test_a_private_chat_event_reaches_only_its_owner(self, client, owner, joiner):
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as mine,
            open_socket(client, joiner["token"], owner["workspace_id"]) as colleague,
        ):
            ready(mine)
            ready(colleague)
            emit(
                client,
                EventType.CHAT_RENAMED,
                owner["workspace_id"],
                {"id": "c1", "title": "first words of a private prompt"},
                recipient_id=owner["user_id"],
            )
            assert next_event(mine)["data"]["title"] == "first words of a private prompt"
            assert_quiet(colleague)

    def test_an_invitation_event_reaches_only_those_who_may_invite(self, client, owner, joiner):
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as admin,
            open_socket(client, joiner["token"], owner["workspace_id"]) as editor,
        ):
            ready(admin)
            ready(editor)
            emit(
                client,
                EventType.INVITATION_SENT,
                owner["workspace_id"],
                {"id": "inv-1"},
                permission="members.invite",
            )
            assert next_event(admin)["type"] == EventType.INVITATION_SENT
            assert_quiet(editor)

    def test_a_promotion_applies_to_the_open_socket(self, client, owner, joiner):
        with open_socket(client, joiner["token"], owner["workspace_id"]) as editor:
            ready(editor)
            emit(
                client,
                EventType.MEMBER_ROLE_CHANGED,
                owner["workspace_id"],
                {"userId": joiner["user_id"], "role": "admin"},
            )
            assert next_event(editor)["type"] == EventType.MEMBER_ROLE_CHANGED
            emit(
                client,
                EventType.INVITATION_SENT,
                owner["workspace_id"],
                {"id": "inv-2"},
                permission="members.invite",
            )
            assert next_event(editor)["type"] == EventType.INVITATION_SENT

    def test_a_removed_member_is_disconnected(self, client, owner, joiner):
        """Otherwise they would keep receiving the workspace after removal."""
        with open_socket(client, owner["token"], owner["workspace_id"]) as admin:
            ready(admin)
            with open_socket(client, joiner["token"], owner["workspace_id"]) as removed:
                frame = ready(removed)
                assert receive(admin)["type"] == EventType.PRESENCE_ONLINE
                connection = connection_of(client, frame)

                emit(
                    client,
                    EventType.MEMBER_REMOVED,
                    owner["workspace_id"],
                    {"id": "m1", "userId": joiner["user_id"]},
                )
                assert close_code(removed) == CLOSE_FORBIDDEN

            assert torn_down(client, connection)
            assert receive(admin)["type"] == EventType.MEMBER_REMOVED
            departure = receive(admin)
            assert departure["type"] == EventType.PRESENCE_OFFLINE
            assert departure["data"]["userId"] == joiner["user_id"]

    def test_replay_applies_the_same_rules(self, client, owner, joiner):
        """A restricted event must not reach someone through the replay path."""
        with open_socket(client, joiner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            start, epoch = position(client, owner["workspace_id"])
            emit(
                client,
                EventType.CHAT_CREATED,
                owner["workspace_id"],
                {"id": "private"},
                recipient_id=owner["user_id"],
            )
            emit(client, EventType.DOCUMENT_CREATED, owner["workspace_id"], {"id": "public"})
            assert next_event(socket)["data"]["id"] == "public"

            frames, report = replay_after(socket, start, epoch)
            assert [f["data"]["id"] for f in frames if f["type"] not in PRESENCE] == ["public"]
            assert report["replayed"] == len(frames)


# ------------------------------------------------------------------ delivery --
class TestDelivery:
    def test_an_event_arrives(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            emit(
                client,
                EventType.DOCUMENT_STATUS,
                owner["workspace_id"],
                {"id": "d1", "status": "indexed"},
            )
            frame = next_event(socket)
            assert frame["type"] == EventType.DOCUMENT_STATUS
            assert frame["data"]["status"] == "indexed"

    def test_the_ready_frame_is_a_usable_baseline(self, client, owner):
        """A socket that receives nothing still knows where it stands."""
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            baseline = ready(socket)["data"]
            event = emit(client, EventType.CHAT_MESSAGE, owner["workspace_id"])
            frame = next_event(socket)
            # +2: the socket's own arrival was sequenced in between.
            assert frame["seq"] == baseline["seq"] + 2 == event.seq
            assert frame["epoch"] == baseline["epoch"]

    def test_events_arrive_in_order(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            for step in (20, 35, 70, 100):
                emit(client, EventType.DOCUMENT_STATUS, owner["workspace_id"], {"progress": step})
            received = [next_event(socket)["data"]["progress"] for _ in range(4)]
            assert received == [20, 35, 70, 100]

    def test_sequences_strictly_increase(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            for _ in range(3):
                emit(client, EventType.CHAT_MESSAGE, owner["workspace_id"])
            sequences = [next_event(socket)["seq"] for _ in range(3)]
            assert sequences == sorted(set(sequences))

    def test_the_actor_is_reported(self, client, owner):
        """So a client can skip the echo of a change it already applied."""
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            emit(client, EventType.DOCUMENT_CREATED, owner["workspace_id"], {"id": "d"}, "someone")
            assert next_event(socket)["actor_id"] == "someone"


# ----------------------------------------------------------------- heartbeat --
class TestHeartbeat:
    def test_ping_is_answered_with_the_marker(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            socket.send_json({"type": "ping", "t": 1234})
            frame = receive(socket)
            assert frame["type"] == EventType.PONG
            # Echoed so the client can measure its own round trip.
            assert frame["data"]["t"] == 1234

    def test_a_ping_refreshes_the_liveness_deadline(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            connection = connection_of(client, ready(socket))
            on_loop(client, setattr, connection, "last_seen_ms", 0)
            assert_quiet(socket)
            assert connection.last_seen_ms > 0

    def test_the_ready_frame_advertises_the_interval(self, client, owner):
        """The client should not have to guess how often to ping."""
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            frame = ready(socket)
            assert frame["data"]["heartbeatSeconds"] == settings.WS_HEARTBEAT_INTERVAL_SECONDS

    def test_junk_frames_are_ignored(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            socket.send_text("not json")
            socket.send_json(["not", "an", "object"])
            socket.send_json({"type": "something-from-the-future"})
            assert_quiet(socket)

    def test_an_oversized_frame_is_refused_without_dropping_the_socket(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            socket.send_text("x" * 5000)
            assert receive(socket)["type"] == EventType.ERROR
            assert_quiet(socket)

    def test_a_silent_connection_is_reaped_and_its_user_goes_offline(self, client, owner, joiner):
        """The path for a laptop that closed its lid: TCP never says goodbye."""
        with open_socket(client, joiner["token"], owner["workspace_id"]) as watcher:
            ready(watcher)
            with open_socket(client, owner["token"], owner["workspace_id"]) as silent:
                connection = connection_of(client, ready(silent))
                assert receive(watcher)["data"]["userId"] == owner["user_id"]

                on_loop(client, setattr, connection, "last_seen_ms", 0)
                assert on_loop(client, get_manager().reap_stale) >= 1
                assert close_code(silent) == CLOSE_TIMEOUT

            assert torn_down(client, connection)
            departure = receive(watcher)
            assert departure["type"] == EventType.PRESENCE_OFFLINE
            assert departure["data"]["userId"] == owner["user_id"]


# ------------------------------------------------------- reconnect and replay --
class TestReconnectAndReplay:
    def test_a_client_receives_what_it_missed(self, client, owner):
        """The point of the replay log: a dropped connection loses nothing."""
        workspace = owner["workspace_id"]
        with open_socket(client, owner["token"], workspace) as socket:
            connection = connection_of(client, ready(socket))
            emit(client, EventType.CHAT_MESSAGE, workspace, {"n": 1})
            seen = next_event(socket)
        assert torn_down(client, connection)

        # Offline: these arrive with nobody listening.
        emit(client, EventType.CHAT_MESSAGE, workspace, {"n": 2})
        emit(client, EventType.CHAT_MESSAGE, workspace, {"n": 3})

        with open_socket(client, owner["token"], workspace) as socket:
            ready(socket)
            frames, report = replay_after(socket, seen["seq"], seen["epoch"])

        replayed = [f["data"]["n"] for f in frames if f["type"] == EventType.CHAT_MESSAGE]
        assert replayed == [2, 3], "events raised while offline were lost"
        assert report["complete"] is True

    def test_replay_is_in_order(self, client, owner):
        workspace = owner["workspace_id"]
        with open_socket(client, owner["token"], workspace) as socket:
            ready(socket)
            start, epoch = position(client, workspace)
            for step in range(5):
                emit(client, EventType.DOCUMENT_STATUS, workspace, {"step": step})
            for _ in range(5):
                next_event(socket)
            frames, _ = replay_after(socket, start, epoch)
        steps = [f["data"]["step"] for f in frames if f["type"] == EventType.DOCUMENT_STATUS]
        assert steps == [0, 1, 2, 3, 4]
        assert [f["seq"] for f in frames] == sorted(f["seq"] for f in frames)

    def test_an_up_to_date_client_is_told_so(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            seq, epoch = position(client, owner["workspace_id"])
            frames, report = replay_after(socket, seq, epoch)
            assert frames == []
            assert report == {**report, "replayed": 0, "complete": True, "seq": seq}

    def test_duplicates_are_identifiable(self, client, owner):
        """A replay window necessarily overlaps what arrived live.

        The server does not try to compute the exact difference -- it cannot
        know what the client actually processed. It guarantees instead that
        every event carries a stable sequence in a stated epoch, so
        re-delivery is detectable and the client drops what it has applied.
        """
        workspace = owner["workspace_id"]
        with open_socket(client, owner["token"], workspace) as socket:
            ready(socket)
            emit(client, EventType.CHAT_MESSAGE, workspace, {"n": 1})
            emit(client, EventType.CHAT_MESSAGE, workspace, {"n": 2})
            live = [next_event(socket) for _ in range(2)]
            frames, _ = replay_after(socket, live[0]["seq"] - 1, live[0]["epoch"])

        assert [(f["seq"], f["epoch"]) for f in frames] == [(f["seq"], f["epoch"]) for f in live]

    def test_a_position_from_another_epoch_forces_a_refetch(self, client, owner):
        """After a counter restart, the old position means nothing here."""
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            emit(client, EventType.CHAT_MESSAGE, owner["workspace_id"])
            next_event(socket)
            frames, report = replay_after(socket, 0, "an-epoch-from-before-a-restart")
            assert frames == []
            assert report["complete"] is False
            assert report["epoch"] != "an-epoch-from-before-a-restart"

    def test_a_trimmed_log_is_reported_incomplete(self, client, owner, monkeypatch):
        """A client away too long is told to refetch, not handed a partial picture."""
        monkeypatch.setattr(settings, "WS_REPLAY_BUFFER_SIZE", 3)
        workspace = owner["workspace_id"]
        with open_socket(client, owner["token"], workspace) as socket:
            ready(socket)
            start, epoch = position(client, workspace)
            for n in range(6):
                emit(client, EventType.CHAT_MESSAGE, workspace, {"n": n})
            for _ in range(6):
                next_event(socket)
            frames, report = replay_after(socket, start, epoch)

        assert report["complete"] is False
        assert [f["data"]["n"] for f in frames] == [3, 4, 5]

    def test_a_malformed_resume_is_harmless(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            socket.send_json({"type": "resume", "after": "not a number"})
            report = receive(socket)
            assert report["type"] == EventType.RESUMED
            assert report["data"]["complete"] is False
            assert_quiet(socket)


# ------------------------------------------------------------- multiple tabs --
class TestMultipleTabs:
    def test_both_tabs_receive_the_event(self, client, owner):
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as first,
            open_socket(client, owner["token"], owner["workspace_id"]) as second,
        ):
            ready(first)
            ready(second, arrival=False)
            emit(client, EventType.DOCUMENT_CREATED, owner["workspace_id"], {"id": "d1"})
            assert next_event(first)["data"]["id"] == "d1"
            assert next_event(second)["data"]["id"] == "d1"

    def test_tabs_get_distinct_connection_ids(self, client, owner):
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as first,
            open_socket(client, owner["token"], owner["workspace_id"]) as second,
        ):
            assert (
                ready(first)["data"]["connectionId"]
                != ready(second, arrival=False)["data"]["connectionId"]
            )

    def test_closing_one_tab_leaves_the_other_working(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as survivor:
            ready(survivor)
            with open_socket(client, owner["token"], owner["workspace_id"]) as doomed:
                connection = connection_of(client, ready(doomed, arrival=False))
            assert torn_down(client, connection)
            emit(client, EventType.CHAT_CREATED, owner["workspace_id"], {"id": "still-here"})
            assert next_event(survivor)["data"]["id"] == "still-here"

    def test_too_many_tabs_are_refused(self, client, owner, monkeypatch):
        """Nothing else caps this: rate limiting never sees a WebSocket."""
        monkeypatch.setattr(get_manager(), "max_per_user", 2)
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as first,
            open_socket(client, owner["token"], owner["workspace_id"]) as second,
        ):
            ready(first)
            ready(second, arrival=False)
            assert rejected(client, owner["token"], owner["workspace_id"]) == CLOSE_TOO_MANY


# ------------------------------------------------------------------- presence --
class TestPresence:
    def test_the_ready_frame_lists_who_is_online(self, client, owner):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            assert owner["user_id"] in ready(socket)["data"]["online"]

    def test_arrival_and_departure_are_announced(self, client, owner, joiner):
        """Two people in one workspace should see each other."""
        with open_socket(client, owner["token"], owner["workspace_id"]) as watcher:
            ready(watcher)
            with open_socket(client, joiner["token"], owner["workspace_id"]) as arriving:
                connection = connection_of(client, ready(arriving))
                arrival = receive(watcher)
                assert arrival["type"] == EventType.PRESENCE_ONLINE
                assert arrival["data"]["userId"] == joiner["user_id"]
                assert joiner["user_id"] in arrival["data"]["online"]

            assert torn_down(client, connection)
            departure = receive(watcher)
            assert departure["type"] == EventType.PRESENCE_OFFLINE
            assert departure["data"]["userId"] == joiner["user_id"]

    def test_a_second_tab_is_not_a_second_arrival(self, client, owner, joiner):
        """Otherwise every reconnect would look like someone joining."""
        with open_socket(client, joiner["token"], owner["workspace_id"]) as watcher:
            ready(watcher)
            with open_socket(client, owner["token"], owner["workspace_id"]) as first:
                ready(first)
                assert receive(watcher)["data"]["userId"] == owner["user_id"]
                with open_socket(client, owner["token"], owner["workspace_id"]) as second:
                    ready(second, arrival=False)
                    assert_quiet(watcher)

    def test_only_the_last_tab_closing_marks_a_user_offline(self, client, owner, joiner):
        with open_socket(client, joiner["token"], owner["workspace_id"]) as watcher:
            ready(watcher)
            with open_socket(client, owner["token"], owner["workspace_id"]) as first:
                first_connection = connection_of(client, ready(first))
                assert receive(watcher)["type"] == EventType.PRESENCE_ONLINE

                with open_socket(client, owner["token"], owner["workspace_id"]) as second:
                    second_connection = connection_of(client, ready(second, arrival=False))
                assert torn_down(client, second_connection)
                # One tab remains: still online, nothing announced.
                assert_quiet(watcher)

            assert torn_down(client, first_connection)
            departure = receive(watcher)
            assert departure["type"] == EventType.PRESENCE_OFFLINE
            assert departure["data"]["userId"] == owner["user_id"]


# ------------------------------------------------------------------ lifecycle --
class TestConnectionLifecycle:
    def test_the_room_empties_when_the_socket_closes(self, client, owner):
        manager = get_manager()
        workspace = owner["workspace_id"]
        with open_socket(client, owner["token"], workspace) as socket:
            connection = connection_of(client, ready(socket))
            assert on_loop(client, manager.room_size, workspace) == 1

        # Waits on the server's handler task, which is what actually does
        # the cleanup -- not on the client having closed its end.
        assert torn_down(client, connection)
        assert on_loop(client, manager.room_size, workspace) == 0
        assert owner["user_id"] not in on_loop(client, manager.users_in, workspace)


# --------------------------------------------------------- broker unavailable --
class TestRedisUnavailable:
    def test_an_unreachable_redis_degrades_to_this_process(self, client, owner):
        """Configured but down: connect and deliver anyway, process-locally."""
        broker = get_broker()
        saved = settings.REDIS_URL
        settings.REDIS_URL = "redis://127.0.0.1:1"
        try:
            with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
                ready(socket)
                emit(client, EventType.DOCUMENT_CREATED, owner["workspace_id"], {"id": "d1"})
                frame = next_event(socket)
                assert frame["data"]["id"] == "d1"
                assert frame["epoch"] == broker.instance_id
        finally:
            settings.REDIS_URL = saved

            async def forget_the_outage() -> None:
                broker._redis = None
                broker._unavailable_until = 0.0

            on_loop(client, forget_the_outage)


# ---------------------------------------------------------- loop ownership --
class TestLifespanOwnership:
    def test_a_second_lifespan_does_not_break_the_running_one(self, client, owner):
        """Regression: a nested lifespan used to stop the process-wide realtime.

        It ran on its own loop, cleared the loop worker threads hand events to,
        and from then on every status update from ingestion was dropped --
        found by the full suite, where TestLifespan runs before these tests.
        """
        import main as app_main
        from app.realtime import notify

        async def second_lifespan() -> None:
            async with app_main.lifespan(app_main.app):
                pass

        asyncio.run(second_lifespan())

        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            # From this thread, which has no loop: the worker-thread path.
            delivered = notify.emit_threadsafe(
                make_event(EventType.DOCUMENT_STATUS, owner["workspace_id"], {"id": "d1"})
            )
            assert delivered is True, "the hand-off to the owning loop was lost"
            assert next_event(socket)["data"]["id"] == "d1"


# ------------------------------------------------------------ across instances --
async def publish_elsewhere(server, event):
    """Publish from a second API instance sharing the same Redis."""
    other = EventBroker(manager=ConnectionManager(), instance_id="another-api-instance")
    other._redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    try:
        return await other.publish(event)
    finally:
        await other.stop()
        await other.manager.stop()


@pytest.fixture
def shared_redis(client):
    """Wire the running application to a Redis shared with other 'instances'.

    fakeredis: a real implementation of the protocol, in process. Restored by
    stopping the broker and starting it again without Redis, through its
    public lifecycle rather than by editing its state.
    """
    server = fakeredis.FakeServer()
    broker = get_broker()
    saved = settings.REDIS_URL
    settings.REDIS_URL = "redis://fake:6379"

    async def attach() -> None:
        await broker.stop()
        broker._redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        await broker.start()
        await asyncio.wait_for(broker.subscribed.wait(), FAIL_AFTER)

    on_loop(client, attach)
    redis_client.set_client(fakeredis.FakeRedis(server=server, decode_responses=True))
    try:
        yield server
    finally:
        redis_client.reset()
        settings.REDIS_URL = saved

        async def detach() -> None:
            await broker.stop()
            await broker.start()

        on_loop(client, detach)


class TestAcrossInstances:
    def test_an_event_from_another_instance_reaches_the_browser(self, client, owner, shared_redis):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            event = make_event(EventType.DOCUMENT_CREATED, owner["workspace_id"], {"id": "far"})
            on_loop(client, publish_elsewhere, shared_redis, event)
            assert next_event(socket)["data"]["id"] == "far"

    def test_this_instance_does_not_deliver_its_own_echo(self, client, owner, shared_redis):
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ready(socket)
            emit(client, EventType.CHAT_MESSAGE, owner["workspace_id"], {"n": "own"})
            sentinel = make_event(EventType.CHAT_MESSAGE, owner["workspace_id"], {"n": "far"})
            on_loop(client, publish_elsewhere, shared_redis, sentinel)
            # The echo of "own" travelled the channel before the sentinel, so
            # if it were delivered it would be here, ahead of it.
            assert [next_event(socket)["data"]["n"] for _ in range(2)] == ["own", "far"]
            assert_quiet(socket)

    def test_the_worker_reaches_the_browser(self, client, owner, shared_redis):
        """A process with no loop and no sockets -- as Celery is -- publishes."""
        with open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            baseline = ready(socket)["data"]
            delivered = publish_from_worker(
                make_event(
                    EventType.DOCUMENT_STATUS,
                    owner["workspace_id"],
                    {"id": "d1", "status": "processing", "progress": 35},
                )
            )
            assert delivered is True
            frame = next_event(socket)
            assert frame["data"]["progress"] == 35
            assert frame["epoch"] == baseline["epoch"], "worker and API share one sequence"

    def test_restricted_events_stay_restricted_across_instances(
        self, client, owner, joiner, shared_redis
    ):
        with (
            open_socket(client, owner["token"], owner["workspace_id"]) as mine,
            open_socket(client, joiner["token"], owner["workspace_id"]) as colleague,
        ):
            ready(mine)
            ready(colleague)
            private = make_event(
                EventType.CHAT_RENAMED,
                owner["workspace_id"],
                {"title": "private"},
                recipient_id=owner["user_id"],
            )
            public = make_event(EventType.DOCUMENT_CREATED, owner["workspace_id"], {"id": "p"})
            on_loop(client, publish_elsewhere, shared_redis, private)
            on_loop(client, publish_elsewhere, shared_redis, public)

            assert next_event(colleague)["data"]["id"] == "p"
            assert next_event(mine)["data"]["title"] == "private"
