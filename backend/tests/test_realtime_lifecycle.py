"""
Authorization over a socket's lifetime, and the realtime lifecycle.

Everything a socket was checked for when it opened can change while it stays
open. These tests change it behind the socket's back -- directly in the
database, as a lost event would leave it -- and prove the re-validation sweep
catches it. They also prove the application's lifespan starts and stops the
realtime layer without leaving tasks or connections behind.

Sweeps are invoked directly on the application's loop, so nothing waits on a
timer; the one test of the periodic schedule waits on the call it causes.
"""

import asyncio
import time

import fakeredis
import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.core.config import settings
from app.realtime import broker as broker_module
from app.realtime import manager as manager_module
from app.realtime.broker import EventBroker, log_key
from app.realtime.events import EventType, make_event
from app.realtime.manager import (
    CLOSE_FORBIDDEN,
    CLOSE_UNAUTHORIZED,
    Connection,
    ConnectionManager,
    get_manager,
)
from app.realtime.revalidation import revalidate
from tests import test_realtime_ws as ws
from tests.test_realtime_manager import FAIL_AFTER, SocketSink

owner = ws.owner
joiner = ws.joiner
shared_redis = ws.shared_redis


def sweep(client) -> dict:
    """One re-validation pass, on the application's own loop."""
    return ws.on_loop(client, revalidate, get_manager())


def _db():
    from app.core.database import SessionLocal

    return SessionLocal()


# ------------------------------------------------------------ token lifetime --
class TestTokenLifetime:
    def test_the_connection_carries_the_tokens_expiry_and_id(self, client, owner):
        from jose import jwt

        claims = jwt.get_unverified_claims(owner["token"])
        with ws.open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            connection = ws.connection_of(client, ws.ready(socket))
            assert connection.token_expires_at == float(claims["exp"])
            assert connection.token_id == claims["jti"]

    def test_an_expired_token_closes_the_socket(self, client, owner):
        """A socket may not outlive the credential it opened with."""
        with ws.open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            connection = ws.connection_of(client, ws.ready(socket))
            ws.on_loop(client, setattr, connection, "token_expires_at", time.time() - 1)

            stats = sweep(client)
            assert stats["expired"] == 1
            assert ws.close_code(socket) == CLOSE_UNAUTHORIZED

    def test_a_current_token_is_left_alone(self, client, owner):
        with ws.open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ws.ready(socket)
            stats = sweep(client)
            assert stats["expired"] == 0 and stats["unauthorized"] == 0
            ws.assert_quiet(socket)

    def test_a_revoked_token_closes_the_socket(self, client, owner, shared_redis):
        """Revocation reaches sockets as well as requests (it needs Redis, as REST does)."""
        from jose import jwt

        from app.core.token_store import revoke_token

        claims = jwt.get_unverified_claims(owner["token"])
        with ws.open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ws.ready(socket)
            assert revoke_token(claims["jti"], claims["exp"]) is True
            stats = sweep(client)
            assert stats["unauthorized"] == 1
            assert ws.close_code(socket) == CLOSE_UNAUTHORIZED


# ------------------------------------------------------- lost-event coverage --
class TestChangesBehindTheSocketsBack:
    """What the sweep exists for: the event that should have acted was lost."""

    def test_a_removed_member_is_disconnected(self, client, owner, joiner):
        from app.models.membership import WorkspaceMember

        with (
            ws.open_socket(client, owner["token"], owner["workspace_id"]) as stays,
            ws.open_socket(client, joiner["token"], owner["workspace_id"]) as removed,
        ):
            ws.ready(stays)
            ws.ready(removed)
            db = _db()
            try:
                db.query(WorkspaceMember).filter(
                    WorkspaceMember.workspace_id == owner["workspace_id"],
                    WorkspaceMember.user_id == joiner["user_id"],
                ).delete()
                db.commit()
            finally:
                db.close()

            stats = sweep(client)
            assert stats["forbidden"] == 1
            assert ws.close_code(removed) == CLOSE_FORBIDDEN

    def test_a_deactivated_account_is_disconnected(self, client, owner):
        from app.models.user import User

        with ws.open_socket(client, owner["token"], owner["workspace_id"]) as socket:
            ws.ready(socket)
            db = _db()
            try:
                db.query(User).filter(User.id == owner["user_id"]).update({"is_active": False})
                db.commit()
            finally:
                db.close()
            try:
                assert sweep(client)["unauthorized"] == 1
                assert ws.close_code(socket) == CLOSE_UNAUTHORIZED
            finally:
                db = _db()
                try:
                    db.query(User).filter(User.id == owner["user_id"]).update({"is_active": True})
                    db.commit()
                finally:
                    db.close()

    def test_a_role_changed_without_an_event_is_brought_up_to_date(self, client, owner, joiner):
        from app.models.membership import WorkspaceMember

        with ws.open_socket(client, joiner["token"], owner["workspace_id"]) as socket:
            ws.ready(socket)
            db = _db()
            try:
                db.query(WorkspaceMember).filter(
                    WorkspaceMember.workspace_id == owner["workspace_id"],
                    WorkspaceMember.user_id == joiner["user_id"],
                ).update({"role": "admin"})
                db.commit()
            finally:
                db.close()

            assert sweep(client)["role_changed"] == 1
            ws.emit(
                client,
                EventType.INVITATION_SENT,
                owner["workspace_id"],
                {"id": "i1"},
                permission="members.invite",
            )
            assert ws.next_event(socket)["type"] == EventType.INVITATION_SENT

    def test_the_other_members_are_unaffected(self, client, owner, joiner):
        from app.models.membership import WorkspaceMember

        with (
            ws.open_socket(client, owner["token"], owner["workspace_id"]) as stays,
            ws.open_socket(client, joiner["token"], owner["workspace_id"]) as removed,
        ):
            ws.ready(stays)
            ws.ready(removed)
            db = _db()
            try:
                db.query(WorkspaceMember).filter(
                    WorkspaceMember.workspace_id == owner["workspace_id"],
                    WorkspaceMember.user_id == joiner["user_id"],
                ).delete()
                db.commit()
            finally:
                db.close()
            sweep(client)
            assert ws.close_code(removed) == CLOSE_FORBIDDEN

            ws.emit(client, EventType.DOCUMENT_CREATED, owner["workspace_id"], {"id": "still"})
            assert ws.next_event(stays)["data"]["id"] == "still"


# ------------------------------------------------- server-initiated close --
class TestServerInitiatedClose:
    def test_the_handler_ends_without_the_clients_cooperation(self, client, owner, joiner):
        """Regression: a socket the server closed kept its handler waiting.

        The handler sat in receive_text() for the client's half of the close
        handshake. Found as a benchmark that could never shut down; in
        production it held the task, and kept the user "online", until the
        server's close timeout. The client here reads nothing at all.
        """
        with ws.open_socket(client, joiner["token"], owner["workspace_id"]) as watcher:
            ws.ready(watcher)
            with ws.open_socket(client, owner["token"], owner["workspace_id"]) as silent:
                connection = ws.connection_of(client, ws.ready(silent))
                assert ws.receive(watcher)["type"] == EventType.PRESENCE_ONLINE

                ws.on_loop(client, get_manager().close, connection, 4400, "heartbeat timeout")
                assert ws.torn_down(client, connection), "the handler waited on the client"

                # And the room heard the departure without the client's help.
                departure = ws.receive(watcher)
                assert departure["type"] == EventType.PRESENCE_OFFLINE
                assert departure["data"]["userId"] == owner["user_id"]


# --------------------------------------------------------- sweep mechanics --
@pytest_asyncio.fixture
async def manager():
    instance = ConnectionManager()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.mark.asyncio
class TestSweepMechanics:
    async def test_nothing_to_check_is_a_no_op(self, manager):
        assert await revalidate(manager) == {
            "checked": 0,
            "expired": 0,
            "unauthorized": 0,
            "forbidden": 0,
            "role_changed": 0,
            "db_errors": 0,
        }

    async def test_expiry_holds_even_when_the_database_does_not_answer(self, manager, monkeypatch):
        """The database blinking must not keep an expired token alive -- nor
        disconnect everybody else."""
        from app.realtime import revalidation

        def unreachable(_snapshot):
            raise ConnectionError("database unreachable")

        monkeypatch.setattr(revalidation, "_judge", unreachable)
        expired = Connection(
            id="expired",
            websocket=SocketSink(),
            user_id="u1",
            workspace_id="ws-1",
            token_expires_at=time.time() - 5,
        )
        current = Connection(
            id="current",
            websocket=SocketSink(),
            user_id="u2",
            workspace_id="ws-1",
            token_expires_at=time.time() + 600,
        )
        await manager.register(expired)
        await manager.register(current)

        stats = await revalidate(manager)
        assert stats["expired"] == 1
        assert stats["db_errors"] == 1
        assert expired.websocket.closed_with[0] == CLOSE_UNAUTHORIZED
        assert current.websocket.closed_with is None
        assert manager.total_connections() == 1

    async def test_the_reaper_runs_the_sweep_on_its_own(self, manager, monkeypatch):
        monkeypatch.setattr(settings, "WS_HEARTBEAT_INTERVAL_SECONDS", 1)
        monkeypatch.setattr(settings, "WS_REVALIDATE_SECONDS", 1)
        called = asyncio.Event()

        async def validator(target):
            assert target is manager
            called.set()

        await manager.start(validator=validator)
        # The interval has a one-second floor; the bound only fails a broken
        # reaper.
        await asyncio.wait_for(called.wait(), timeout=5)


# ------------------------------------------------------------ the lifespan --
def test_a_full_lifespan_leaves_no_tasks_and_no_redis_connection(monkeypatch):
    """Start and stop realtime exactly as the application does, with Redis.

    On its own loop, with its own manager and broker, so the process-wide ones
    the other tests share are untouched. A connection is registered whose
    teardown publishes through the broker, as the endpoint's does: shutdown
    must let that publish reach Redis *before* the broker stops -- stopping
    the broker first made it reopen a connection nothing closed.
    """
    import main as app_main

    server = fakeredis.FakeServer()
    monkeypatch.setattr(settings, "REDIS_URL", "redis://fake:6379")
    fresh_manager = ConnectionManager()
    fresh_broker = EventBroker(manager=fresh_manager, instance_id="lifecycle-test")
    monkeypatch.setattr(manager_module, "_manager", fresh_manager)
    monkeypatch.setattr(broker_module, "_broker", fresh_broker)

    async def run():
        fresh_broker._redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        async with app_main.lifespan(app_main.app):
            await asyncio.wait_for(fresh_broker.subscribed.wait(), FAIL_AFTER)
            assert fresh_manager._validator is revalidate

            sink = SocketSink()
            connection = Connection(id="c1", websocket=sink, user_id="u1", workspace_id="ws-lc")

            async def handler():
                # Shaped like the endpoint's: cancelled once its connection is
                # closed, announcing the departure in its finally block.
                try:
                    await asyncio.Event().wait()  # the client, which never answers
                except asyncio.CancelledError:
                    pass
                finally:
                    await fresh_broker.publish(
                        make_event(EventType.PRESENCE_OFFLINE, "ws-lc", {"userId": "u1"})
                    )

            connection.handler = asyncio.create_task(handler())
            await fresh_manager.register(connection)

        me = asyncio.current_task()
        leftovers = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
        return leftovers, connection

    leftovers, connection = asyncio.run(run())

    assert leftovers == [], f"tasks survived shutdown: {leftovers}"
    assert connection.handler.done()
    assert connection.websocket.closed_with[0] == 1001
    # The departure reached Redis while it was still up...
    stored = fakeredis.FakeRedis(server=server, decode_responses=True).lrange(
        log_key("ws-lc"), 0, -1
    )
    assert len(stored) == 1
    # ...and nothing tried to reconnect afterwards.
    assert fresh_broker.stats["redis_errors"] == 0
    assert fresh_broker._redis is None
    assert fresh_broker.subscribed.is_set() is False
    assert fresh_manager.total_connections() == 0
