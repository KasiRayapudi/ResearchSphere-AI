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

`global-setup.ts` waits for `/api/ready`. In CI it also requires the database,
Redis and Qdrant to be reachable and fails immediately if they are not.
Locally it reports what is missing and carries on, so a partial stack can be
used deliberately -- the tests that need those services will fail on their own
terms rather than being skipped.

## Accounts and data

Each test registers its own account through the API, and signup creates that
account's workspace. Passwords are generated per account at run time, so no
credentials are committed and no test depends on data that already exists.
Documents uploaded by a test carry unique content, which keeps them clear of
the duplicate detection the upload pipeline performs.

## Determinism

There are no fixed sleeps. Waits are on application state: readiness, URLs,
visible status text, and the WebSocket frames the browser actually receives.
Retries are off, so a test that only passes on a second attempt shows up as a
failure rather than hiding as a flake.
