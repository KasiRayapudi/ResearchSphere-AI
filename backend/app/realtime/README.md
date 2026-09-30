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

## Events

Every frame from the server is one JSON envelope:
`{type, workspace_id, data, seq, epoch, ts, actor_id, recipient_id, permission, v}`.

- `type` comes from a closed vocabulary (`events.py`): `document.created`,
  `document.status`, `document.deleted`; `member.added`, `member.removed`,
  `member.role_changed`; `invitation.sent`, `invitation.revoked`,
  `invitation.accepted`; `chat.created`, `chat.renamed`, `chat.deleted`,
  `chat.message`; `presence.online`, `presence.offline`. The
  connection-level frames `connection.ready`, `connection.pong`,
  `connection.error` and `connection.resumed` carry no sequence number and
  are never replayed.
- `data` mirrors the REST response for the same object, so a client applies
  it directly to state it loaded over HTTP.
- `seq` and `epoch` place the event in its workspace's order (see below).
- `actor_id` is the user whose action caused the event, so a tab can skip the
  echo of its own change. `recipient_id` and `permission` narrow the audience
  (see Audience), and the server enforces them.
- `v` is the envelope version, currently 1.

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

The server can send `connection.ready` again on an open socket. It does so
when its instance was cut off from the others -- its Redis subscription was
lost and has come back -- so events published meanwhile never reached this
socket. A client treats it exactly as the first: in the same epoch it
resumes, in a new one it refetches. No new frame type is involved.

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
  REST.** `complete` is true only when the epoch matches and *every* sequence
  number after `N`, up to `seq`, is still in the log (bounded by
  `WS_REPLAY_BUFFER_SIZE`). The log is appended in the order publishers
  finish, not the order numbers were issued, so "its oldest entry is old
  enough" is not the same test. Clients must not infer loss from gaps in
  what they receive: restricted events leave gaps by design.

## Across instances

Each API instance holds its own sockets, and Redis pub/sub connects them. A
publish is sequenced (`INCR`, with the epoch created by `SET NX` in the same
MULTI/EXEC), appended to the workspace's capped replay log, delivered at
once to this instance's sockets, and then published on one shared channel
tagged with this instance's id, so the instance ignores its own echo. The
Celery worker, which has no sockets and no event loop, does the same with
the blocking client and leaves delivery to the API instances. Logout
revocations travel on the same channel as control messages, which are never
delivered to a client.

The subscription is kept alive deliberately:

- the instance counts as listening only once the server has confirmed
  SUBSCRIBE;
- the channel is read in 1 s slices, so a quiet channel is never mistaken
  for a dead connection (`pubsub.listen()` read under the 5 s command timeout,
  which ended every subscription after five quiet seconds);
- after 15 s with nothing received the instance pings Redis, and no reply
  within 5 s ends the connection;
- reconnects back off from 1 s, doubling to 30 s and reset once subscribed,
  and the lost subscription is closed before the wait;
- a Redis client the instance gives up on -- after a failed call or a lost
  subscription -- is closed with its connection pool, not left for garbage
  collection, and stopping waits for those closes;
- a subscription that replaces a lost one re-sends `connection.ready` to the
  instance's sockets, so their clients resume what they missed (see
  Connecting).

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

### Isolation and privacy

- A socket joins only the workspace it was authorised for, and every event
  names its workspace. Delivery checks both, so one workspace's events cannot
  reach another's sockets, whether published locally or received over Redis.
- Replay applies the same audience check as live delivery, against the
  socket's current role. A resume therefore cannot reveal what a demoted
  member may no longer see; a removed member's sockets are closed, and
  reconnecting is refused at the handshake.
- Payloads carry no secrets. Invitation events carry only an id -- never the
  invitation token, email or role. Chat titles, which are prompt text, go
  only to the owner. Member events carry the email the members list already
  shows to every role.
- Logs record user, connection and workspace ids, a refused Origin, event
  types and error text. They never record tokens, prompts, passwords or email
  addresses.
- Every instance receives every payload over Redis, restricted ones
  included, so Redis sits inside the same trust boundary as the database.

## Heartbeat and cleanup

The client sends `{"type":"ping","t":<value>}` every `heartbeatSeconds`
(announced in `connection.ready`, default 25), and the server answers
`connection.pong` with the same `t` so the client can measure its round trip.
Any client frame refreshes the server's deadline; a connection silent for
`WS_HEARTBEAT_TIMEOUT_SECONDS` (default 60) is closed with 4400. The client
gives a connection up after two intervals plus 5 s with nothing from the
server, without waiting for a close handshake a dead path may never
complete. Unknown client frames are ignored; a frame over 4 KB is answered
with `connection.error`, and the socket stays open.

## Presence

A user is online in a workspace while at least one of their sockets is open
there, on any instance. Presence is a set of connection ids per user per
workspace, not a flag: a second tab, a phone, or a reconnect that overlaps
the socket it replaces is the same person. Only the first connection
announces `presence.online`, and only the last to close announces
`presence.offline`; both carry the full `online` list, and `connection.ready`
carries it too. The sets live in Redis with a 90 s TTL that each ping
refreshes, so a crashed instance cannot leave its users online, and a
departure records last-seen. Without Redis, presence is this process's
sockets.

## Authorization over a socket's lifetime

A socket is authorised when it opens -- signature, expiry, type, revocation,
active account, membership -- but it can then stay open for hours. Two
mechanisms keep it honest.

**At the moment it happens:**

- the token **expires** (4401): each socket carries a timer set for its
  token's `exp`. The client refreshes and reconnects, and resume makes that
  invisible;
- the token is **revoked by a logout** (4401): the logout closes every socket
  opened with that token -- `ConnectionManager.close_token` here, and on the
  other instances through `EventBroker.revoke_token`'s control message on the
  Redis channel. Other tokens of the same user are untouched, exactly as REST
  treats them;
- the member is **removed** (4403) or their **role changes**, applied as the
  `member.*` event is delivered.

A client that reconnects is authorised from scratch, so a revoked token, an
inactive account or a removed member cannot come back.

**Every `WS_REVALIDATE_SECONDS`** (default 60), as the backstop, the reaper
runs `revalidation.revalidate` for whatever the above missed: a clock that
jumped, an event or control message lost to a Redis blip, an account
deactivated directly in the database. It closes sockets whose token has
expired or was revoked or whose account is gone or inactive (4401), or whose
membership is gone (4403), and brings roles up to date. Expiry is checked in
process, so it holds even while the database is unreachable. If the database
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
| Redis configured but down, or not answering | Any failed call -- a connect (2 s timeout) or a command (5 s timeout), presence included -- skips Redis for 5 s, so a handshake pays for at most one failure per window rather than one per call. Delivery continues process-locally. The subscriber reconnects with backoff: 1 s doubling to 30 s, reset once subscribed. |
| Subscription lost, or silently dead | A quiet subscription reads in 1 s slices and is never ended for being quiet. After 15 s with nothing received it pings Redis, and no answer within 5 s ends the connection -- which also keeps load balancers and NAT gateways from dropping it silently. A subscription that comes back after being lost re-sends `connection.ready` to this instance's sockets, so clients resume what other instances published meanwhile. |
| Worker cannot publish | Logged and dropped. Indexing is unaffected, and the upload queue asks REST once on its next resync. |
| Database down | New sockets are refused (authentication needs it). Existing sockets stay; expiry is still enforced. |
| A second application lifespan in one process | The realtime singletons belong to the loop that started them. A start or stop from another loop is a no-op while the owner lives. |

## Limits

Every `BaseHTTPMiddleware` returns early for non-HTTP scopes. Rate limiting,
request ids, logging, metrics and security headers therefore never see a
WebSocket. The limits here are the only ones there are:
`WS_MAX_CONNECTIONS_PER_USER` (default 5) and `WS_MAX_CONNECTIONS_TOTAL`
(default 1000 per process), both refused with 4429; a 4 KB maximum client
frame; and `WS_SEND_QUEUE_SIZE` (default 100 frames) per connection, beyond
which a client too slow to keep up is closed with 4408.

## Known limits

- **One channel for every workspace.** Each instance receives every event
  and discards the ones for rooms it is not serving. Isolation does not
  depend on that: the room lookup and the audience rules run before anything
  reaches a socket. It is a cost, paid by every instance for every event in
  the deployment. Measured in process on a development laptop -- a parsing
  cost, not a production capacity figure -- discarding a 600-byte
  `document.created` took about 11 µs (roughly 90,000 events per
  core-second), before the bytes on the wire. At a few thousand events per
  second deployment-wide that is noise. Per-workspace or sharded channels
  become worthwhile when the deployment-wide rate reaches tens of thousands
  per second, when discarded traffic is a visible share of an instance's CPU,
  or when bursts approach Redis's pub/sub output-buffer limit (by default a
  subscriber 8 MB behind for 60 s, or 32 MB at once, is disconnected --
  recovered by re-subscribing and re-announcing, at the price of every client
  resuming at once).
- **Real-Redis coverage runs in CI only.** `tests/test_realtime_redis.py`
  runs the broker, presence and the endpoint against a real server when
  `REALTIME_REDIS_URL` is set -- in CI against `redis:7-alpine`, locally
  against a throwaway instance such as `docker compose up -d redis` -- and is
  skipped without it. The CI step fails unless every test ran and passed with
  none skipped (counted from JUnit XML), and if any connection was left for
  the garbage collector to close. Elsewhere the suite uses fakeredis in process, which
  applies no read timeouts, cannot stop answering, and keeps a subscription
  registered after its connection closes.
- **Untested topologies.** Nothing exercises a Redis restart with
  persistence reload, Sentinel or Cluster failover, or a real multi-process
  deployment behind a reverse proxy (WebSocket upgrade headers, `wss`
  termination, proxy idle timeouts).
- **Presence scans the keyspace.** `presence.online` finds a workspace's
  presence sets with `SCAN MATCH`, which walks every key in the database in
  batches of 100, on every handshake and every departure. On a Redis shared
  with Celery results, caches and the token blacklist, that cost grows with
  the whole keyspace rather than the workspace. A per-workspace index of
  present users is the fix once the keyspace reaches the hundreds of
  thousands.
- **No real-browser test.** The client is tested in jsdom against a
  scripted socket, and the server through Starlette's test client. Nothing
  drives a real browser against a live server.
- **Chat events have no screen.** `chat.*` events reach the owner's tabs, but
  no page lists chat sessions, so nothing renders them; the chat page
  consumes document events only.
- Chat token streaming stays on SSE for the requesting tab. The socket
  carries only that a message was stored.
