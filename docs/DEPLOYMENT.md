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

`.env.prod.example` is tracked, so a fresh clone contains the template:

```bash
cp .env.prod.example .env.prod
```

Every secret-bearing value in it is empty and must be filled in. `.env.prod`
itself stays ignored by `.gitignore` and must never be committed.

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

The stack runs two application images, under one name each:

```
ghcr.io/kasirayapudi/researchsphere-ai/backend:${IMAGE_TAG}
ghcr.io/kasirayapudi/researchsphere-ai/frontend:${IMAGE_TAG}
```

`backend`, `worker` and `beat` all run the backend image with different
commands, so there is a single artefact to build, scan and promote.

Those names are used in **both** modes, which differ only in where the image
comes from:

| Mode | Command | Image source | Files needed on the host |
|---|---|---|---|
| Published images | `make prod-pull` | Pulled from GHCR | `docker-compose.prod.yml`, `nginx/`, `.env.prod` |
| Local build | `make prod` | Built from this working tree | The above **plus** `backend/` and `frontend/` |

`docker-compose.prod.yml` carries no `build:` stanza, so the published-image
mode needs no application source. The build instructions live in
`docker-compose.prod.build.yml`, applied as an overlay; `make prod` and
`make prod-build` use it. A locally built image is tagged with exactly the same
name as a published one, so nothing downstream has to know which mode produced
it — set `IMAGE_TAG=local` to keep them apart by eye.

**The published-image mode is not source-free.** The nginx service bind-mounts
`./nginx/nginx.conf`, `./nginx/conf.d` and `./nginx/certs` from the deployment
directory, so a host needs the `nginx/` directory alongside
`docker-compose.prod.yml` and `.env.prod`. What it does not need is the
application source or any build toolchain. The GitHub release attaches
`docker-compose.prod.yml` and `.env.prod.example` but **not** `nginx/`, so the
published release artefact alone is not yet sufficient — see
[Known deployment gaps](#11-known-deployment-gaps).

### Selecting a version

| Thing | Format | Example |
|---|---|---|
| Git tag | `vMAJOR.MINOR.PATCH` | `v1.2.0` |
| Published image tags | `MAJOR.MINOR.PATCH`, `MAJOR.MINOR`, `MAJOR`, `sha-<40 hex>` | `1.2.0`, `1.2`, `1` |
| `IMAGE_TAG` for a release | the Git tag **without** its leading `v` | `1.2.0` |
| `IMAGE_TAG` for a local build | any label; the template ships `local` | `local` |

There is **no `latest` tag**. `release.yml` runs only on tags, and production
pins a version deliberately rather than following a moving target. `IMAGE_TAG`
has no default: Compose refuses to start without it, and `make prod-pull`
refuses to run when it is unset or still `local`.

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
| `frontend` | `…/frontend:${IMAGE_TAG}` | Serves the built SPA on `8080` | `frontend_net` | `curl --fail http://127.0.0.1:8080/healthz` |
| `backend` | `…/backend:${IMAGE_TAG}` | FastAPI API on `8000` (`serve`) | `backend_net` | `curl --fail http://127.0.0.1:8000/api/ready` |
| `worker` | same image, command `worker` | Celery worker: document extraction, chunking, embedding and the Qdrant upsert | `backend_net` | `celery -A app.worker.celery_app:celery_app inspect ping` |
| `beat` | same image, command `beat` | Celery beat scheduler for the periodic reclaim of documents whose worker died. **Exactly one may run per deployment** | `backend_net` | freshness of beat's schedule file (see [§8](#8-health-and-validation)) |
| `postgres` | `postgres:16-alpine` | Relational store; volume `postgres_data` | `backend_net` | `pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}` |
| `qdrant` | `qdrant/qdrant:v1.8.0` | Vector store; volume `qdrant_data` | `backend_net` | `GET /readyz` over bash's `/dev/tcp` |
| `redis` | `redis:7-alpine` | Celery broker, realtime pub/sub and rate-limit counters. Started with `--requirepass`, `--appendonly yes`, `--maxmemory ${REDIS_MAXMEMORY:-256mb}` and `--maxmemory-policy noeviction`; volume `redis_data` | `backend_net` | `redis-cli -a "$REDIS_PASSWORD" ping` |

`backend` and `worker` share `uploads_data:/app/uploads` (so the worker can read
what the API stored) and `model_cache:/home/appuser/.cache` (so the embedding
model is downloaded once).

Every dependency edge is health-gated with one deliberate exception:

| Edge | Condition |
|---|---|
| `nginx` → `backend`, `frontend` | `service_healthy` |
| `backend` → `postgres`, `qdrant`, `redis` | `service_healthy` |
| `worker` → `postgres`, `qdrant`, `redis` | `service_healthy` |
| `beat` → `postgres`, `qdrant`, `redis` | `service_healthy` |
| `beat` → `worker` | `service_started` — beat only publishes to the broker, and a queued task waits perfectly well |

---

## 6. Deployment Procedure

Every target below is defined in the `Makefile`. Targets that do not build
expand to `docker compose -f docker-compose.prod.yml --env-file .env.prod …`;
`prod` and `prod-build` add `-f docker-compose.prod.build.yml`.

### 1. Create `.env.prod`

```bash
cp .env.prod.example .env.prod
```

Fill in every value marked REQUIRED — see [§3](#3-production-configuration) —
and set `IMAGE_TAG` per [§4](#4-production-images). `make prod` and
`make prod-pull` both exit immediately if the file is absent.

### 2. Obtain the images

Either pull a published release:

```bash
make prod-pull      # pulls IMAGE_TAG and starts the stack with --no-build
```

or build from this working tree:

```bash
make prod-build     # build only
```

`make prod-pull` refuses to run when `IMAGE_TAG` is unset or still `local`, and
starts with `--no-build` so a deployment cannot silently fall back to building.
If you used `prod-pull`, skip to step 4 — it has already started the stack.

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

Checks that `.env.prod` exists, then builds from this tree and starts the stack.
For a published release use `make prod-pull` instead, which starts the same
topology from GHCR images without building.

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
| `/api/live` | Liveness only — performs no dependency I/O. Not used as a gate |
| `/api/health` | Dependency detail: database, qdrant, redis, gemini, storage, disk, memory |
| `/api/ready` | Readiness gate. Returns `503` unless every dependency is in an acceptable state. **Used by the backend container healthcheck**, in both `docker-compose.prod.yml` and `backend/Dockerfile.prod` |
| `/healthz` (frontend image, port 8080) | Served by the frontend image; used by its healthcheck |
| `/healthz` (edge nginx) | `return 200 'ok'`, with access logging off |

> `/api/ready` also considers the Gemini key: with `GEMINI_API_KEY` unset the
> check reports `missing` and readiness fails. In this stack that variable is
> required, so a correctly configured deployment satisfies it.

### Compose healthchecks

**All eight services declare one** (see the table in
[§5](#5-production-compose-stack)). Three are worth explaining.

`backend` probes `/api/ready`, not `/api/live`. nginx gates on the backend being
healthy, so "healthy" has to mean "can actually serve"; `/api/live` would report
an instance healthy with its database or vector store unreachable. Resource
pressure deliberately does not fail readiness, so a disk at 85% warns through
`/api/health` rather than pulling the instance out.

The probe also sets its `Host` header to the first entry of `TRUSTED_HOSTS`, and
that is not cosmetic. `TrustedHostMiddleware` fronts every route and answers
`400` for a `Host` outside `TRUSTED_HOSTS`, health endpoints included. Probing
`http://127.0.0.1:8000/...` sends `Host: 127.0.0.1:8000`, which no real
`TRUSTED_HOSTS` contains, so the probe was rejected and the container could
never become healthy. Deriving the header from the configured value keeps the
probe valid for any deployment without widening the allowlist. The same applies
to the image's own `HEALTHCHECK` in `backend/Dockerfile.prod`, which falls back
to `localhost` when the variable is unset.

`qdrant` has no HTTP client in its image — neither `curl` nor `wget` is present
— but its Debian base does provide `bash`, so the probe issues a real HTTP
request through bash's `/dev/tcp` against Qdrant's own `/readyz` and requires a
`200`. `/readyz` sits on Qdrant's API-key whitelist, which means it answers even
when Qdrant is configured to require a key — so a healthy `qdrant` container is
**not** evidence that the backend can query it. Qdrant runs unauthenticated
here: no API key is configured, because Qdrant enforces one as soon as the
setting is present at all (an empty value included) and the backend has no key
to send. Its protection is the network — it publishes no port and is reachable
only from `backend_net`.

`beat` watches the freshness of its own schedule file. A process check would be
meaningless — beat is the container's `exec`'d main process, so its death takes
the container with it — and the failure worth catching is a scheduler that runs
but stops ticking. Celery's `PersistentScheduler` writes that file at startup
and re-syncs it from the service loop at least every 300s, so a file that has
not moved in 900s means the loop stopped. The path is set explicitly with
`--schedule` in `backend/docker-entrypoint.sh`; the entrypoint and the
healthcheck must agree on it.

---

## 9. Nginx and TLS

### What is configured today

- `nginx/nginx.conf` defines the rate-limit zones: `api_limit` at 30 r/s and
  `auth_limit` at 5 r/s, both keyed on `$binary_remote_addr`.
- `nginx/nginx.conf` also defines `map $http_upgrade $connection_upgrade`, the
  standard mapping a WebSocket proxy needs.
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

### Realtime WebSocket

`location ^~ /api/v1/ws` proxies the application's only WebSocket, forwarding
`Upgrade` and `Connection: $connection_upgrade`, with buffering off and 3600s
read/send timeouts — far beyond the 25s application heartbeat, so the
application's own 60s heartbeat timeout decides when a socket is dead rather
than the proxy.

Two details matter if this block is ever edited:

- `^~` rather than a plain prefix. Prefix matching already beats `/api/` on
  length, but `^~` also stops any regex location from claiming the upgrade
  request and proxying it without the `Upgrade` header.
- The six shared `proxy_set_header` directives are repeated inside the location.
  nginx inherits `proxy_set_header` from the enclosing level **only when the
  current level declares none**, so setting `Upgrade` there would otherwise drop
  `Host` and the `X-Forwarded-*` set — and losing `Host` makes the backend's
  TrustedHost middleware reject the connection. `Origin` is deliberately not
  declared: it is an ordinary request header nginx forwards unchanged, which is
  what the backend checks against `CORS_ORIGINS`.

The CI job `prod-stack` opens a real WebSocket through this proxy on every run
and requires the upgrade, the echoed `bearer` subprotocol and a first event
frame.

---

## 10. GHCR Release Workflow

`.github/workflows/release.yml` defines the publishing pipeline.

| Aspect | Current definition |
|---|---|
| Trigger | Push of a tag matching `v*.*.*`, or `workflow_dispatch` with a `tag` input |
| Permissions | `contents: write`, `packages: write`, `id-token: write`, `attestations: write` |
| Job 1 — `verify` | Runs the backend tests, then the frontend tests and build, against the tagged commit |
| Job 2 — `publish` | Needs `verify`. Matrix over `backend` (`backend/Dockerfile.prod`) and `frontend` (`frontend/Dockerfile.prod`); Buildx; logs in to `ghcr.io`; derives tags with `docker/metadata-action` as `{{version}}`, `{{major}}.{{minor}}`, `{{major}}` and long SHA — **no `latest`**; builds with `push: true`, `provenance: true`, `sbom: true`; then `actions/attest-build-provenance` with `push-to-registry: true` |
| Job 3 — `release` | Needs `publish`. Creates a GitHub release with `softprops/action-gh-release`, whose notes include `docker pull` lines for both images and a link back to this document |
| Registry | `ghcr.io/kasirayapudi/researchsphere-ai/{backend,frontend}`. The name is lowercase because OCI repository names must be; it comes from `IMAGE_NAMESPACE` in the workflow env, which `docker-compose.prod.yml` mirrors verbatim |

> **Status — never exercised.** No tag exists in this repository, locally or on
> the remote, so `release.yml` has never run and no image has been published to
> GHCR. Everything above describes the workflow definition, not an observed run.

---

## 11. Known Deployment Gaps

These are the gaps established from the repository itself. They are recorded so
the deployment path is not mistaken for something more finished than it is.

| # | Gap | Evidence |
|---|---|---|
| 1 | The release workflow has never been exercised | No tag exists locally or on the remote, so no image has ever been published and **pulling from GHCR has never actually been performed**. The CI job builds the images under their Compose names and starts the stack with `--no-build`, which verifies the published-image *path* but not a real registry pull |
| 2 | TLS is disabled | The 443 server block, the HTTP→HTTPS redirect and the `HTTPS_PORT` mapping are all commented out; no certificates are provisioned |
| 3 | The release artefact is not self-sufficient | `release.yml` attaches `docker-compose.prod.yml` and `.env.prod.example`, but nginx bind-mounts three paths from `./nginx`, which is not attached. A host still needs those files from the repository |
| 4 | Kubernetes support is absent | No manifests, chart or overlays exist |

### Closed since the previous revision

| Gap | How |
|---|---|
| GHCR images were not consumed by the production stack | `docker-compose.prod.yml` now names the GHCR images and carries no `build:` stanza; the build instructions moved to `docker-compose.prod.build.yml` and `make prod-pull` was added |
| `beat` and `qdrant` had no healthcheck | Both now have one — see [§8](#8-health-and-validation) — and every consumer gates on `service_healthy` |
| The edge did not forward WebSocket upgrades | `map $http_upgrade $connection_upgrade` plus `location ^~ /api/v1/ws` — see [§9](#9-nginx-and-tls) |
| The SSE/chat location dropped the shared proxy headers | `location /api/v1/chat/stream` now repeats `Host` and the `X-Forwarded-*` set alongside `Connection ''`, for the same nginx inheritance reason as the WebSocket location — fixed in `5b3a857` |
| `.env.prod.example` was not tracked | `.gitignore` now re-includes it and the template is committed, with every secret-bearing value empty |
| The Compose stack was never started by CI | The `prod-stack` job starts all eight services, waits for every healthcheck, drives the edge and opens a real WebSocket through it |
| `beat` declared `deploy:` twice | The duplicate — a copy of the worker's limits — was removed; beat keeps 0.25 CPU / 256M |
| The backend healthcheck probed `/api/live` while claiming readiness | Both `docker-compose.prod.yml` and `backend/Dockerfile.prod` now probe `/api/ready` |
| The release workflow named an uppercase repository | `IMAGE_NAMESPACE` supplies the lowercase name to the attestation subject and the release notes |
| `latest` was referenced but never published | The dead metadata rule was removed and `IMAGE_TAG` is now explicit — see [§4](#4-production-images) |

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
`docker-compose.prod.yml`, `docker-compose.prod.build.yml`,
`backend/Dockerfile.prod`, `frontend/Dockerfile.prod`,
`backend/docker-entrypoint.sh`, `.env.prod.example`, `backend/.env.example`,
`.gitignore`, `Makefile`, `.github/workflows/release.yml`,
`.github/workflows/ci.yml`, `nginx/nginx.conf`,
`nginx/conf.d/researchsphere.conf` and `README.md`.

**What has been verified, and how:**

| Claim | Verified by |
|---|---|
| The Compose files parse and the topology starts | The `prod-stack` CI job — `docker compose config` on both file sets, then all eight services healthy |
| The edge proxies the API, serves the SPA and upgrades WebSockets | The same job, against the running stack |
| The datastores are not reachable from the host | The same job |
| The nginx configuration is syntactically valid in every context | Parsed with NGINX's own parser (crossplane) |
| The qdrant and beat healthcheck commands work | Executed locally against live and dead endpoints, in both the healthy and unhealthy cases |
| `/api/ready` gates on dependencies | The repository's own test suite |
| The release workflow's structure is unchanged apart from the naming fix | Parsed and compared against the previous revision |

**What has not been verified:** no real deployment has been performed, no image
has been pulled from GHCR, and TLS has never been enabled. `release.yml` has
still never run.
