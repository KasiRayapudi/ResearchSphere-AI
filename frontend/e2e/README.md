# End-to-end tests

Playwright drives a real browser against the real application: the Vite dev
server, the API, PostgreSQL, Redis, a Celery worker and Qdrant. Nothing in the
main flows is mocked -- not the API, not the WebSocket, not ingestion.

The dev server is used because it proxies `/api` **and** the WebSocket upgrade
to the backend, so the browser's requests are same-origin exactly as they are
behind the production reverse proxy. The production bundle is covered by the
Docker job in `.github/workflows/ci.yml`.

## Running them

Playwright starts the frontend itself. Everything behind it must already be
running:

```bash
# from the repository root: PostgreSQL, Redis and Qdrant
docker compose up -d postgres redis qdrant

# backend (in backend/, with its virtualenv active)
alembic upgrade head
uvicorn main:app --port 8000

# Celery worker, so uploads are processed off the request
python -m celery -A app.worker.celery_app:celery_app worker --loglevel info --concurrency 2

# the suite (in frontend/)
npm run test:e2e
```

`global-setup.ts` waits for `/api/ready`. In CI it requires the database, Redis
and Qdrant to report `ok` and fails the run immediately if any of them does
not. It asserts those three by name rather than the endpoint's status code,
because `/api/ready` also covers `GEMINI_API_KEY`, which none of these journeys
use and which CI holds no key for: a completely healthy end-to-end stack still
answers 503 there. Locally the setup reports what is missing and carries on, so
a partial stack can be used deliberately -- the tests that need those services
fail on their own terms rather than being skipped.

In CI the whole stack is provided by the `End-to-end (browser)` job in
`.github/workflows/ci.yml`, which runs Postgres, Redis and Qdrant as services
and starts the API and a Celery worker before the browser does anything.

## What a local partial stack shows

Without Qdrant, an upload is still validated, stored and picked up for
ingestion, and the document reaches `queued` and `processing` -- then indexing
fails at the vector upsert and the document ends as `failed`. So a local run
proves the browser-to-API-to-storage half of the journey; `indexed`, the vector
write and Celery-backed processing are only exercised where the full stack
runs, which is the CI job above.

`indexing.spec.ts` therefore declares itself not runnable when those services
are absent, rather than being left to fail every local run. That declaration is
switched off in CI: `CI` is set on the job, and global setup has already failed
the run if a service is unreachable, so the indexed journey cannot quietly not
run. Its assertion is not relaxed anywhere -- `queued` and `processing` are
never accepted, and `failed` fails the test rather than waiting out the clock.
What that job proves beyond the green test is checked separately by its
`Prove the indexing pipeline ran` step, which looks for this document's id in
the worker's log and in Qdrant itself.

## Proving realtime rather than polling

`realtime.spec.ts` reads the frames of the application's own WebSocket through
Playwright -- it does not inject events, intercept routes or touch React state.
It requires that a `document.status` frame for the exact document it uploaded
arrived *before* the row changed, that the browser also saw the document in a
non-terminal state first, and that the one endpoint the UI could have polled
with, `GET /documents/{id}/status`, was called at most once while waiting.

That allowance is not slack: `UploadQueue` makes one REST read per indexing
document, and only after a resync the server reported incomplete. Repeated
reads are polling and fail the test. Note that a dev-server run also opens
Vite's own HMR socket, so the spec selects the socket whose URL contains
`/api/v1/ws`.

## Proving recovery after a dropped socket

`reconnect.spec.ts` interrupts the application socket mid-session and keeps it
down while a document is uploaded and fully indexed, so the page cannot have
heard about that transition. Then it lets the client's own retry through and
requires that the client *resumed* in the same sequence space, that the server
replayed what it missed and called the replay complete, and that the row
reached `indexed` from a replayed frame on the new socket -- with no reload, no
document-list refetch and no status read after the reconnect.

The interruption uses Playwright's `routeWebSocket`, scoped to `/api/v1/ws`, in
pass-through mode. Worth knowing before changing it:

- It replaces the page's `WebSocket` class with a shim, and the real
  connection is opened inside it with the page's own token and Origin. Every
  frame is still the server's, forwarded unmodified; the route only decides
  when the connection ends and when a retry may reach the server.
- Closing a route closes one side only. The spec closes the page side with
  1006 (what a browser reports for a dropped connection) and then the server
  side with 1000, because `WebSocket.close()` rejects 1006. The other order
  would hand the page a clean close forwarded from the server first.
- `context.setOffline()` is not an alternative on the dev server: it also
  drops Vite's HMR socket, and Vite reloads the page when that reconnects.

One status read during the outage is expected and asserted: `UploadQueue` asks
once for an upload that returned while the socket was down. After the
reconnect there must be none.

## Why CI runs the suite in two legs

The API allows 20 requests a minute per client IP across `/auth/*` and
`/upload` together. Each browser test costs at least two of them -- the SPA
checks `/auth/me` on mount, and React StrictMode runs that effect twice on the
dev server -- and an upload costs one more. All E2E traffic comes from one IP,
so the whole suite in one job would exceed the limit. CI therefore runs it as
two legs, each with its own complete stack and so its own limit window; the
limit itself is unchanged. `E2E_CORE_SPECS` and `E2E_REALTIME_SPECS` in the
workflow say which spec runs where, and the job fails if a spec file is in
neither list or in both.

## Accounts and data

Each test registers its own account through the API, and signup creates that
account's workspace. Passwords are generated per account at run time, so no
credentials are committed and no test depends on data that already exists. The
generator alternates a letter with a non-letter so that it cannot accidentally
produce something the password policy rejects -- a character repeated three
times, or a run like `abcd` -- which a purely random string does about 7% of
the time.
Documents uploaded by a test carry unique content, which keeps them clear of
the duplicate detection the upload pipeline performs.

## Rate limiting

The API limits every `/auth/` path to 20 requests a minute per client IP, and
the SPA calls `/auth/me` on each mount. One run of this suite costs roughly ten
of those requests, so it fits comfortably -- but two runs started inside the
same minute do not, and the second one fails with the app refusing logins.
That is the rate limiter doing its job, not a flaky test.

Signup and login therefore happen once per worker, and specs that are not about
the sign-in form start from a page seeded with those tokens through the same
`localStorage` contract the app uses. When running the suite repeatedly by
hand, leave a minute between runs.

## Determinism

There are no fixed sleeps. Waits are on application state: readiness, URLs,
visible status text, and the WebSocket frames the browser actually receives.
Retries are off, so a test that only passes on a second attempt shows up as a
failure rather than hiding as a flake.
