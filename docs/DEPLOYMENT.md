# Production Deployment

This document describes how ResearchSphere AI is deployed **as the repository
stands today**. It records the supported path and, equally, the parts that are
scaffolded but not yet finished. Where something is incomplete it is marked as
such rather than described as if it worked.

The supported production target is a **single host running Docker Compose**,
driven by `docker-compose.prod.yml` and the `Makefile`. Kubernetes is on the
roadmap but is not implemented; see [Kubernetes](#12-kubernetes).

---

## 1. Overview

The production stack serves the React single-page application and the FastAPI
API behind one nginx edge, and processes document ingestion asynchronously in a
Celery worker.

| Layer | Services |
|---|---|
| Edge | `nginx` |
| Application | `frontend`, `backend` |
| Background work | `worker`, `beat` |
| State | `postgres`, `redis`, `qdrant` |

Two networks separate the tiers: `frontend_net` (nginx ↔ frontend) and
`backend_net` (nginx ↔ backend, and the backend tier ↔ its datastores). Only
nginx publishes a port to the host.

Persistent data lives in named volumes: `postgres_data`, `qdrant_data`,
`redis_data`, `uploads_data`, `model_cache`, `nginx_logs`, `certbot_www`.

---

## 2. Prerequisites

| Requirement | Detail |
|---|---|
| Docker Compose v2 | Every `Makefile` target invokes `docker compose` (the v2 subcommand), not `docker-compose` |
| A `.env.prod` file | At the repository root. `make prod` refuses to start without it |
| Secrets | `SECRET_KEY`, database and Redis passwords, and a Gemini API key — see below |
| Disk | Volumes for Postgres, Qdrant, Redis, uploads and the embedding model cache |
| Ports | `HTTP_PORT` (default `80`) is published by nginx. No other service publishes a port |

> **Note — the `.env.prod` template is not in the repository.** `make prod`
> prints `copy .env.prod.example and fill it in`, but `.env.prod.example` is
> **not tracked**: `.gitignore` ignores `.env.*` and only re-includes
> `.env.example`. A fresh clone will not contain it. Use the variable tables in
> [§3](#3-production-configuration) to write `.env.prod` by hand.

---

## 3. Production Configuration

`.env.prod` is read by Compose through `--env-file .env.prod`. It supplies the
values that `docker-compose.prod.yml` interpolates; the backend and worker
containers receive their settings from that file's `environment:` blocks, not
from `.env.prod` directly.

### Required values

Compose declares these with `:?`, so **the stack refuses to start if any is
missing or empty**.

| Variable | Used for |
|---|---|
| `SECRET_KEY` | JWT signing. The application also fails fast on an unset key |
| `POSTGRES_USER` | Postgres role; also composed into `DATABASE_URL` |
| `POSTGRES_PASSWORD` | Postgres password; also composed into `DATABASE_URL` |
| `POSTGRES_DB` | Database name; also composed into `DATABASE_URL` |
| `REDIS_PASSWORD` | Passed to `redis-server --requirepass` and composed into `REDIS_URL` |
| `GEMINI_API_KEY` | Chat and research generation. Also required for `/api/ready` to report ready |
| `CORS_ORIGINS` | Allowed browser origins; also the allow-list for WebSocket `Origin` checks |
| `FRONTEND_URL` | Absolute URL of the deployed frontend |
| `TRUSTED_HOSTS` | `TrustedHostMiddleware` allow-list |

### Optional values (defaults applied by Compose)

| Variable | Default |
|---|---|
| `IMAGE_TAG` | `latest` |
| `HTTP_PORT` | `80` |
| `WEB_CONCURRENCY` | `4` |
| `GRACEFUL_TIMEOUT` | `30` |
| `CELERY_CONCURRENCY` | `2` |
| `MAX_UPLOAD_SIZE_MB` | `50` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `10080` |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` |
| `QDRANT_COLLECTION` | `researchsphere_docs` |
| `QDRANT_API_KEY` | empty |
| `REDIS_MAXMEMORY` | `256mb` |

### Values Compose derives for you

Do not set these in `.env.prod`; `docker-compose.prod.yml` builds them from the
variables above and the service names on `backend_net`:

```
DATABASE_URL=postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
QDRANT_HOST=qdrant
QDRANT_PORT=6333
UPLOAD_DIR=/app/uploads
ENVIRONMENT=production
```

### Generating `SECRET_KEY`

```bash
make secret
```

That target runs `openssl rand -base64 48` and prints one value. It does not
write to any file — copy the output into `.env.prod` yourself.

> **Never commit secrets.** `.gitignore` already excludes `.env.prod` and
> `.env.*`. The only tracked template is `backend/.env.example`, which lists
> every application setting the backend understands and contains no real
> values.

---

## 4. Production Images

Two image paths exist today, and **they are not wired together**.

| Path | Names | Produced by | Consumed by |
|---|---|---|---|
| Local build | `researchsphere/backend:${IMAGE_TAG:-latest}`, `researchsphere/frontend:${IMAGE_TAG:-latest}` | `make prod-build` / `make prod` (Compose `build:` stanzas) | `docker-compose.prod.yml` |
| Registry | `ghcr.io/<owner>/<repo>/backend`, `ghcr.io/<owner>/<repo>/frontend` | `.github/workflows/release.yml` on a `v*.*.*` tag | **nothing in this repository** |

`docker-compose.prod.yml` references the local names only and carries `build:`
stanzas for `frontend`, `backend` and `worker`, so `make prod` builds images on
the host. Pulling the GHCR images into the Compose stack is not supported by
any file in the repository today; see
[Known deployment gaps](#11-known-deployment-gaps).

Both production images are built from dedicated Dockerfiles and run unprivileged:

| Image | Base | Runs as | Listens on |
|---|---|---|---|
| `backend/Dockerfile.prod` | `python:3.11-slim` (multi-stage) | `appuser`, uid `10001` | `8000` |
| `frontend/Dockerfile.prod` | `nginxinc/nginx-unprivileged:1.27-alpine` (built with `node:20-alpine`) | uid `101` | `8080` |

Both declare a container `HEALTHCHECK`. The backend entrypoint is
`tini -- /app/docker-entrypoint.sh` with the default command `serve`.

---

## 5. Production Compose Stack

Services defined by `docker-compose.prod.yml`. All use `restart: unless-stopped`.

| Service | Image | Role | Network | Compose healthcheck |
|---|---|---|---|---|
| `nginx` | `nginx:1.27-alpine` | Edge reverse proxy; the only service publishing a host port (`${HTTP_PORT:-80}:80`); routes `/api/` to the backend and everything else to the frontend | `frontend_net`, `backend_net` | `wget --spider http://127.0.0.1/healthz` |
| `frontend` | `researchsphere/frontend:${IMAGE_TAG:-latest}` | Serves the built SPA on `8080` | `frontend_net` | `curl --fail http://127.0.0.1:8080/healthz` |
| `backend` | `researchsphere/backend:${IMAGE_TAG:-latest}` | FastAPI API on `8000` (`serve`) | `backend_net` | `curl --fail http://127.0.0.1:8000/api/live` |
| `worker` | same image, command `worker` | Celery worker: document extraction, chunking, embedding and the Qdrant upsert | `backend_net` | `celery -A app.worker.celery_app:celery_app inspect ping` |
| `beat` | same image, command `beat` | Celery beat scheduler for the periodic reclaim of documents whose worker died. **Exactly one may run per deployment** | `backend_net` | **none** |
| `postgres` | `postgres:16-alpine` | Relational store; volume `postgres_data` | `backend_net` | `pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}` |
| `qdrant` | `qdrant/qdrant:v1.8.0` | Vector store; volume `qdrant_data` | `backend_net` | **none** |
| `redis` | `redis:7-alpine` | Celery broker, realtime pub/sub and rate-limit counters. Started with `--requirepass`, `--appendonly yes`, `--maxmemory ${REDIS_MAXMEMORY:-256mb}` and `--maxmemory-policy noeviction`; volume `redis_data` | `backend_net` | `redis-cli -a "$REDIS_PASSWORD" ping` |

`backend` and `worker` share `uploads_data:/app/uploads` (so the worker can read
what the API stored) and `model_cache:/home/appuser/.cache` (so the embedding
model is downloaded once).

---

## 6. Deployment Procedure

Every target below is defined in the `Makefile` and expands to
`docker compose -f docker-compose.prod.yml --env-file .env.prod …`.

### 1. Create `.env.prod`

Write the file at the repository root using the tables in
[§3](#3-production-configuration). `make prod` exits immediately if it is absent.

### 2. Build the images

```bash
make prod-build
```

Runs `docker compose … build`. Optional in principle, because `make prod` also
builds, but running it first surfaces build failures before anything starts.

### 3. Validate the configuration

```bash
make validate
```

Runs `docker compose … run --rm --no-deps backend validate`, which executes the
image's `validate` entrypoint (`python -m app.cli.validate_config`) without
starting the datastores. It requires the backend image to exist, so run it after
step 2.

### 4. Apply database migrations

```bash
make prod-migrate
```

See [§7](#7-database-migrations). This starts the datastore dependencies,
because it is a plain `run` (not `--no-deps`).

### 5. Start the stack

```bash
make prod
```

Checks that `.env.prod` exists, then runs `docker compose … up -d --build`.

### 6. Check service health

```bash
make prod-ps
```

Runs `docker compose … ps`, which reports each service's health state.

### Day-to-day operations

| Command | Effect |
|---|---|
| `make prod-logs` | `logs -f --tail=100` across the stack |
| `make prod-worker-logs` | `logs -f worker beat` |
| `make prod-down` | `down`. Named volumes are preserved |
| `make prod-stamp` | Marks an existing pre-Alembic database as current **without** migrating |

---

## 7. Database Migrations

The application does not create tables on start-up; it checks the schema
revision and refuses to run against a schema it was not built for. Migrations
are therefore an explicit deployment step.

```bash
make prod-migrate
```

expands to:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm backend migrate
```

The image's `migrate` entrypoint waits for its dependencies, validates the
configuration, then runs `python -m alembic upgrade head`.

For a database that predates Alembic in this project, `make prod-stamp` runs the
`stamp` entrypoint, which records the current revision **without applying any
migration**. Use it only when the existing schema is known to match head.

`make migrate` and `make migrate-status` target the **development** stack
(`docker-compose.yml`), not production.

---

## 8. Health and Validation

### Application endpoints

| Endpoint | Meaning |
|---|---|
| `/api/live` | Liveness. Used by the backend container healthcheck |
| `/api/health` | Dependency detail: database, qdrant, redis, gemini, storage, disk, memory |
| `/api/ready` | Readiness gate. Returns `503` unless every dependency is in an acceptable state |
| `/healthz` (frontend image, port 8080) | Served by the frontend image; used by its healthcheck |
| `/healthz` (edge nginx) | `return 200 'ok'`, with access logging off |

> `/api/ready` also considers the Gemini key: with `GEMINI_API_KEY` unset the
> check reports `missing` and readiness fails. In this stack that variable is
> required, so a correctly configured deployment satisfies it.

### Compose healthchecks

Present for `nginx`, `frontend`, `backend`, `worker`, `postgres` and `redis`
(see the table in [§5](#5-production-compose-stack)).

> **Warning — two services have no healthcheck:** `beat` and `qdrant`.
> `docker compose ps` will not report health for them, and nothing waits on
> their readiness. For `qdrant`, the official image carries no shell probe
> tools, which is why the same limitation appears in CI, where the E2E job waits
> for Qdrant from outside the container instead.

---

## 9. Nginx and TLS

### What is configured today

- `nginx/nginx.conf` defines the rate-limit zones: `api_limit` at 30 r/s and
  `auth_limit` at 5 r/s, both keyed on `$binary_remote_addr`.
- `nginx/conf.d/researchsphere.conf` defines upstreams `backend:8000` and
  `frontend:8080`, sets `proxy_http_version 1.1`, and serves a single
  `server { listen 80; server_name _; }`.
- Routing: a streaming location for `/api/v1/chat/stream` (with
  `proxy_set_header Connection ''`), rate-limited locations for
  `/api/v1/auth/(login|signup|refresh|password-reset)`,
  `/api/v1/documents/upload` (with `client_max_body_size 64m`), the report and
  research entrypoints, `/api/`, the API docs, and a catch-all `/` to the
  frontend.
- The ACME HTTP-01 challenge location `/.well-known/acme-challenge/` is active
  and served from `/var/www/certbot`, backed by the `certbot_www` volume.
- `./nginx/certs` is mounted read-only into the container at `/etc/nginx/certs`.

### What is **not** enabled

> **Warning — this deployment serves plain HTTP.**
>
> - The `listen 443 ssl` server block in `nginx/conf.d/researchsphere.conf` is
>   **commented out**, including its `ssl_certificate` and
>   `ssl_certificate_key` directives.
> - The HTTP→HTTPS redirect (`return 301 https://$host$request_uri;`) is
>   **commented out**.
> - The `${HTTPS_PORT:-443}:443` port mapping in `docker-compose.prod.yml` is
>   **commented out**.
> - `nginx/certs/` contains only a `.gitkeep`; no certificate is provisioned,
>   and nothing automates issuance or renewal.
>
> Enabling TLS means supplying certificates and uncommenting those blocks. That
> work is **not** part of this document and has not been done.

> **Warning — the edge does not forward WebSocket upgrades.** The realtime
> endpoint `/api/v1/ws` is matched by `location /api/`, but no configuration in
> `nginx/` sets `proxy_set_header Upgrade` / `Connection "upgrade"`, and there is
> no `$http_upgrade` map. The end-to-end realtime tests exercise the Vite dev
> server's proxy, not this file, so the edge's WebSocket behaviour is unverified
> as configured.

---

## 10. GHCR Release Workflow

`.github/workflows/release.yml` defines the publishing pipeline.

| Aspect | Current definition |
|---|---|
| Trigger | Push of a tag matching `v*.*.*`, or `workflow_dispatch` with a `tag` input |
| Permissions | `contents: write`, `packages: write`, `id-token: write` |
| Job 1 — `verify` | Runs the backend tests, then the frontend tests and build, against the tagged commit |
| Job 2 — `publish` | Needs `verify`. Matrix over `backend` (`backend/Dockerfile.prod`) and `frontend` (`frontend/Dockerfile.prod`); Buildx; logs in to `ghcr.io`; derives tags with `docker/metadata-action` as `{{version}}`, `{{major}}.{{minor}}`, `{{major}}`, long SHA, and `latest` on the default branch; builds with `push: true`, `provenance: true`, `sbom: true`; then `actions/attest-build-provenance` with `push-to-registry: true` |
| Job 3 — `release` | Needs `publish`. Creates a GitHub release with `softprops/action-gh-release`, whose notes include `docker pull` lines for both images and a link back to this document |
| Registry | `ghcr.io/<owner>/<repo>/backend` and `…/frontend` |

> **Status — never exercised.** No tag exists in this repository, locally or on
> the remote, so `release.yml` has never run and no image has been published to
> GHCR. Everything above describes the workflow definition, not an observed run.

---

## 11. Known Deployment Gaps

These are the gaps established from the repository itself. They are recorded so
the deployment path is not mistaken for something more finished than it is.

| # | Gap | Evidence |
|---|---|---|
| 1 | The release workflow has never been exercised | No tag exists locally or on the remote |
| 2 | GHCR images are not consumed by the production stack | `docker-compose.prod.yml` references `researchsphere/*` with `build:` stanzas and never mentions `ghcr.io` |
| 3 | TLS is disabled | The 443 server block, the HTTP→HTTPS redirect and the `HTTPS_PORT` mapping are all commented out; no certificates are provisioned |
| 4 | `beat` and `qdrant` have no healthcheck | `docker-compose.prod.yml` |
| 5 | The edge does not forward WebSocket upgrades | No `Upgrade`/`Connection: upgrade` headers and no `$http_upgrade` map anywhere in `nginx/` |
| 6 | `.env.prod.example` is not tracked | `.gitignore` ignores `.env.*` and re-includes only `.env.example`, yet `make prod` tells you to copy it |
| 7 | The Compose stack is never started by CI | CI builds both production images and smoke-tests them individually; the E2E job runs its own services rather than this Compose topology, so `docker-compose.prod.yml` itself is unverified |
| 8 | Kubernetes support is absent | No manifests, chart or overlays exist |

---

## 12. Kubernetes

Kubernetes support is one of the three items the README lists under Phase 5,
alongside the CI/CD pipeline and production deployment.

**It is not implemented.** The repository contains no Kubernetes manifests, Helm
chart, Kustomize overlays or any other orchestration descriptor, and no workflow
deploys to a cluster. There is nothing to apply, and this document deliberately
offers no example manifests: they would describe software that does not exist
here yet.

---

## Verification status of this document

Every command, service name, environment variable, image name, port and workflow
behaviour above was read from the files it describes:
`docker-compose.prod.yml`, `backend/Dockerfile.prod`,
`frontend/Dockerfile.prod`, `backend/docker-entrypoint.sh`,
`backend/.env.example`, `Makefile`, `.github/workflows/release.yml`,
`nginx/nginx.conf`, `nginx/conf.d/researchsphere.conf` and `README.md`.

No part of this document has been validated by performing a real deployment.
