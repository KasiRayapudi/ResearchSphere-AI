"""
Re-checking live sockets against the current state of the world.

A WebSocket is authorised once, when it opens, and can then stay open for
hours. Everything checked at that moment can change afterwards: the token
expires or is revoked, the account is deactivated, the member is removed or
their role changes. Events announce most of those changes, but an event can
be lost -- a Redis blip at the moment a member is removed -- and a token
expiring announces nothing at all.

So the manager's reaper calls ``revalidate`` periodically
(WS_REVALIDATE_SECONDS). It closes:

* sockets whose token has expired -- 4401, so the client refreshes and
  reconnects with a new one. Checked in process, from the token's own exp
  claim, so it happens even when the database does not answer;
* sockets whose token was revoked, or whose account is gone or inactive --
  4401;
* sockets whose user is no longer a member of the workspace -- 4403.

and brings each surviving socket's role up to date.

**When the database cannot be reached, a sweep keeps the sockets** it could
not check and tries again next time. Closing every connection because the
database blinked would start a reconnect storm that could not authenticate
anyway; the expiry check above still runs, so no socket outlives its token.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.logging import get_logger

from .manager import CLOSE_FORBIDDEN, CLOSE_UNAUTHORIZED, ConnectionManager

logger = get_logger("realtime.revalidation")


@dataclass(frozen=True)
class Verdict:
    connection_id: str
    close_code: int | None = None
    reason: str = ""
    role: str | None = None


def _judge(snapshot: list[tuple[str, str, str, str | None]]) -> list[Verdict]:
    """Decide each connection's fate. Blocking: run in a thread.

    Two queries however many sockets there are -- memberships and accounts
    for every user and workspace in the snapshot -- plus one revocation
    lookup per token, which is a Redis read and a no-op without Redis, as it
    is for the REST API.
    """
    from app.core.database import SessionLocal
    from app.core.token_store import is_token_revoked
    from app.models.membership import WorkspaceMember
    from app.models.user import User

    user_ids = {user_id for _, user_id, _, _ in snapshot}
    workspace_ids = {workspace_id for _, _, workspace_id, _ in snapshot}

    db = SessionLocal()
    try:
        roles = {
            (row.workspace_id, row.user_id): row.role
            for row in db.query(
                WorkspaceMember.workspace_id, WorkspaceMember.user_id, WorkspaceMember.role
            ).filter(
                WorkspaceMember.workspace_id.in_(workspace_ids),
                WorkspaceMember.user_id.in_(user_ids),
            )
        }
        active = {
            row.id: bool(row.is_active)
            for row in db.query(User.id, User.is_active).filter(User.id.in_(user_ids))
        }
    finally:
        db.close()

    verdicts = []
    for connection_id, user_id, workspace_id, token_id in snapshot:
        if not active.get(user_id, False):
            verdicts.append(Verdict(connection_id, CLOSE_UNAUTHORIZED, "account unavailable"))
        elif token_id and is_token_revoked(token_id):
            verdicts.append(Verdict(connection_id, CLOSE_UNAUTHORIZED, "token revoked"))
        elif (workspace_id, user_id) not in roles:
            verdicts.append(Verdict(connection_id, CLOSE_FORBIDDEN, "membership revoked"))
        else:
            verdicts.append(Verdict(connection_id, role=roles[(workspace_id, user_id)]))
    return verdicts


async def revalidate(manager: ConnectionManager) -> dict[str, int]:
    """Close sockets that are no longer entitled to stay open. Returns counts."""
    from fastapi.concurrency import run_in_threadpool

    stats = {
        "checked": 0,
        "expired": 0,
        "unauthorized": 0,
        "forbidden": 0,
        "role_changed": 0,
        # Sweeps that could not reach the database; the error itself is logged.
        "db_errors": 0,
    }
    live = manager.live()
    stats["checked"] = len(live)
    if not live:
        return stats

    # Expiry first, and without the database: this is the check that must
    # hold even when nothing else can be verified.
    now = time.time()
    current = []
    for connection in live:
        if connection.token_expires_at is not None and connection.token_expires_at <= now:
            stats["expired"] += 1
            await manager.close(connection, CLOSE_UNAUTHORIZED, "token expired")
        else:
            current.append(connection)
    if not current:
        return stats

    snapshot = [(c.id, c.user_id, c.workspace_id, c.token_id) for c in current]
    try:
        verdicts = await run_in_threadpool(_judge, snapshot)
    except Exception as exc:
        logger.error(f"Realtime revalidation could not reach the database: {exc}")
        stats["db_errors"] += 1
        return stats

    by_id = {c.id: c for c in current}
    for verdict in verdicts:
        target = by_id.get(verdict.connection_id)
        if target is None or target.closing:
            continue
        if verdict.close_code is not None:
            key = "forbidden" if verdict.close_code == CLOSE_FORBIDDEN else "unauthorized"
            stats[key] += 1
            logger.info(
                f"Closing connection {target.id}: {verdict.reason}",
                extra={"user_id": target.user_id, "action": "realtime.revalidate"},
            )
            await manager.close(target, verdict.close_code, verdict.reason)
        elif verdict.role != target.role:
            target.role = verdict.role
            stats["role_changed"] += 1
    return stats


__all__ = ["Verdict", "revalidate"]
