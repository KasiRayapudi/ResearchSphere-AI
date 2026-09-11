# Realtime

Live workspace events over one WebSocket per open workspace. It replaces the
2.5-second status polling the upload queue used, and adds live updates that
had no mechanism at all before: collaboration changes, other people's
uploads and deletions, chat changes in a user's other tabs, and presence.

The REST API stays the source of truth. The socket only tells a browser that
something happened. Every state change still goes through REST, with its
authorization, validation and audit.

## Pieces

| Module | Responsibility |
|---|---|
| `events.py` | The envelope and the closed vocabulary of event types. |
| `manager.py` | Sockets held by this process: one room per workspace, a writer task per connection, audience rules, limits, heartbeat reaping, drain on shutdown. |
| `broker.py` | Sequencing, the replay log, and Redis pub/sub fan-out across instances. Degrades to process-local without Redis. |
| `presence.py` | Who is online, as a set of connection ids per user, shared through Redis. |
| `notify.py` | What producers call. Builds the event with the right audience and never raises. |
| `app/api/v1/websocket.py` | The endpoint: authentication, origin check, membership, resume. |

## Connecting

```js
new WebSocket(`wss://host/api/v1/ws?workspace_id=${id}`, ['bearer', accessToken])
```

- **The token travels in the subprotocol, never in the URL.** Query strings
  are written to access logs, proxy logs and browser history. A token in the
  URL is ignored.
- The token is validated exactly as the REST API validates it: signature,
  expiry, issuer, audience, type (a refresh token is refused) and revocation.
  The user must be active **and a member of the workspace**.
- `Origin` is checked against `CORS_ORIGINS`. Browsers do not apply CORS to
  WebSockets, so this is the only place the check happens. A request with no
  Origin, which means it is not from a browser, is allowed.

The first frame is `connection.ready`:
`{connectionId, userId, protocol, heartbeatSeconds, online, seq, epoch}`.

## Close codes

| Code | Meaning | What the client should do |
|---|---|---|
| 4401 | Token missing, invalid, expired or refused | Refresh once, then stop |
| 4403 | Not a member, origin refused, disabled, or **membership revoked while connected** | Stop; reload workspaces |
| 4429 | Per-user or process connection limit reached | Back off (at least 10 s) |
| 4408 | Client too slow to keep up | Reconnect and resume |
| 4400 | Heartbeat timeout | Reconnect and resume |
| 1001 | Server shutting down | Reconnect and resume |

## Ordering, duplicates and replay

Every workspace event carries `seq` and `epoch`.

- `seq` comes from a per-workspace Redis `INCR`, so every instance and the
  Celery worker share one total order.
- `epoch` names the sequence space. The counter can restart: every process
  restart without Redis, or Redis losing its data. A client comparing a new
  `seq: 1` against its remembered `seq: 37` would otherwise discard
  everything as duplicates. The sequence and epoch keys do **not** expire:
  a counter that lapsed under an open tab would restart silently.
- **Delivery order is arrival order, not sequence order.** Two events
  published at nearly the same moment on different instances can arrive
  crosswise. Clients therefore drop *exact* repeats (by seq), never
  "everything at or below the highest seen", and where order matters they
  keep it per entity.
- To resume, the client sends `{"type":"resume","after":N,"epoch":E}`. The
  server replays what the client may see and ends with `connection.resumed`
  `{complete, replayed, seq, epoch}`. **`complete: false` means refetch over
  REST.** That happens when the epoch differs or the log (bounded by
  `WS_REPLAY_BUFFER_SIZE`) no longer reaches back far enough. Clients must
  not infer loss from gaps: restricted events leave gaps by design.

## Audience

An event goes to the workspace room, narrowed by the same rules the REST API
applies to the same data. The manager enforces this through `may_receive`,
for live delivery and replay alike.

| Events | Who receives them | Why |
|---|---|---|
| `document.*`, `member.*` | Every member | Readable by every role over REST |
| `chat.*` | The session owner's own connections only | `GET /chat/sessions` filters on the owner; titles are prompt text |
| `invitation.*` | Roles with `members.invite`, and **only the id** | `GET /invitations` requires that permission; the list is refetched so authorization is re-applied at read time |
| `presence.*` | Every member | Presence is room-scoped; never crosses workspaces |

Membership changes apply to live sockets. `member.role_changed` updates the
connection's role before delivery. `member.removed` closes that user's
sockets in the workspace with 4403.

## Heartbeat and cleanup

The client pings every `heartbeatSeconds`. Any frame refreshes the server's
deadline, and connections silent for `WS_HEARTBEAT_TIMEOUT_SECONDS` are
closed. The client treats two missed intervals as a dead path and
reconnects. Presence entries carry a TTL as well, so a crashed instance
cannot leave users online forever.

## Authorization over a socket's lifetime

A socket is authorised when it opens, but it can then stay open for hours.
Every `WS_REVALIDATE_SECONDS` (default 60) the reaper runs
`revalidation.revalidate`, which closes sockets whose:

- **token has expired** (4401). This is checked in process from the token's
  `exp` claim, so it holds even while the database is unreachable. The
  client refreshes and reconnects, and resume makes that invisible.
- **token was revoked** (4401). This needs Redis, as it does for REST.
- **account is gone or inactive** (4401).
- **membership is gone** (4403). This covers a `member.removed` event lost to
  a Redis blip.

It also brings each surviving socket's role up to date. If the database
cannot be reached, a sweep keeps the sockets it could not check and retries
next time. Disconnecting everyone during an outage would start a reconnect
storm that could not authenticate anyway.

## The browser client

`frontend/src/services/realtime.ts`, owned by `RealtimeProvider`:

- connects while signed in with an active workspace, reconnects when the
  workspace changes, and closes on sign-out or unmount;
- reconnects with exponential backoff and jitter (0.5 s doubling to a 30 s
  ceiling, reset on success). On 4429 it waits at least 10 s. When the
  browser reports the network is back or the tab becomes visible, it skips
  the remaining backoff;
- on 4401, refreshes the token once and reconnects; if that is refused
  again, it stops. On 4403 it stops and the workspace list is reloaded;
- pings every `heartbeatSeconds` and abandons a connection that has been
  silent for two intervals plus slack, without waiting for a close handshake
  that a dead path may never complete;
- resumes from a little behind its highest sequence seen, drops exact
  repeats, and emits `realtime.resync` so screens refetch when it cannot
  prove continuity: on first connect, after an incomplete resume, and on a
  new epoch.

## Degradation

| Failure | Behaviour |
|---|---|
| No Redis configured | Supported for a single process. Events, replay and presence are process-local. With no Celery broker, ingestion runs inline and hands events to the API loop directly. |
| Redis configured but down | After one failed connect (2 s timeout), Redis is skipped for 5 s. Handshakes never wait on it longer than that one attempt, and delivery continues process-locally. The subscriber reconnects with backoff (1 s to 30 s). |
| Worker cannot publish | Logged and dropped. Indexing is unaffected, and the upload queue asks REST once on its next resync. |
| Database down | New sockets are refused (authentication needs it). Existing sockets stay; expiry is still enforced. |
| A second application lifespan in one process | The realtime singletons belong to the loop that started them. A start or stop from another loop is a no-op while the owner lives. |

## Limits

Every `BaseHTTPMiddleware` returns early for non-HTTP scopes. Rate limiting,
request ids, logging, metrics and security headers therefore never see a
WebSocket. The limits here are the only ones there are:
`WS_MAX_CONNECTIONS_PER_USER`, `WS_MAX_CONNECTIONS_TOTAL`, a 4 KB maximum
client frame, and a bounded per-connection send queue.

## Known limits

- **One channel for every workspace.** Each instance receives every event
  and discards the ones for rooms it is not serving. That is simple and
  race-free, but the cost grows with instances × event rate. Per-workspace
  channels are the upgrade path.
- **Not verified against a real Redis server.** Tests use fakeredis, a real
  in-process implementation of the protocol, so cross-instance fan-out,
  sequencing and presence are exercised. Reconnection against a genuinely
  restarting Redis is not.
- Chat token streaming stays on SSE for the requesting tab. The socket
  carries only that a message was stored.
