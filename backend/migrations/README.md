# Database migrations

The schema is owned by **Alembic**. The application never creates or alters
tables: it checks the recorded revision at startup and refuses to run against
a schema it was not built for.

Migration scripts live in `backend/alembic/versions/`. The historical manual
SQL below is kept for databases that predate Alembic.

---

## Which command do I run?

This is the only decision that matters, and getting it wrong is destructive.

| Your database | Command | Why |
|---|---|---|
| Brand new / empty | `alembic upgrade head` | Creates every table. |
| Already managed by Alembic | `alembic upgrade head` | Applies whatever is outstanding. |
| **Has tables but no `alembic_version`** | `alembic stamp head` | The schema already exists. `upgrade` would try to `CREATE TABLE` over live tables and fail. |

`alembic stamp head` writes the version number and **changes nothing else**.
It is a statement that the schema already matches the head revision. Only use
it when that is true.

To find out which case you are in:

```sql
SELECT version_num FROM alembic_version;
```

- Table missing → you are in the third row (or the first, if the database is
  empty).
- A row returned → you are in the second row.

The application tells you the same thing on startup and names the command to
run, so if you are unsure, start it once and read the error.

### Existing deployments upgrading to the Alembic release

A database built by the old `create_all()` startup path has all ten tables
and no `alembic_version`. It needs to be stamped once, and only once:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm backend stamp
# or: make prod-stamp
```

Before stamping, confirm the schema really does match the baseline — that
means migrations 001-003 below have all been applied by hand. If any is
missing, apply that SQL first, then stamp. Stamping a schema that does not
match the head is how you get a runtime failure weeks later on a column that
was never added.

After stamping, every future release is just:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm backend migrate
# or: make prod-migrate
```

---

## Everyday commands

```bash
make migrate            # apply pending migrations (development)
make migrate-status     # show current and head revisions
make migration m="add widget table"   # autogenerate from model changes
make prod-migrate       # apply pending migrations (production)
```

Directly, from `backend/`:

```bash
alembic current -v            # what the database is at
alembic heads                 # what the code expects
alembic history               # the full chain
alembic upgrade head          # apply
alembic upgrade head --sql    # print the SQL instead of running it
alembic downgrade -1          # go back one revision
```

`alembic upgrade head --sql` is the option to reach for when a DBA has to
review the change before it touches production.

### Adding a migration

1. Change the model under `app/models/`.
2. `make migration m="what changed"`.
3. **Read the generated file.** Autogenerate is a starting point, not an
   answer: it does not detect renames (it emits a drop plus an add, which
   loses the data), and it cannot know what to backfill.
4. Check the `downgrade()` is correct, or the change cannot be rolled back.
5. Run `alembic upgrade head`, then `alembic downgrade -1`, then
   `alembic upgrade head` again against a scratch database.

The test suite builds its schema by running the migrations, so a migration
that does not match the models fails the whole suite rather than production.

---

## Deployment order

The API refuses to start against a schema that is behind, so migrations must
run first:

1. `backend migrate` as a one-shot task.
2. Roll out the new API and worker images.

For a migration that is not backwards compatible with the running version,
use the usual two-step: deploy a release that tolerates both shapes, migrate,
then deploy the release that requires the new shape.

### Rolling back

`alembic downgrade` runs the `downgrade()` of each revision in reverse.
**Downgrading past the baseline drops every table and all data** — it is the
reverse of "create the schema". Roll back application versions freely; roll
back the schema only with a backup in hand.

Starting an old build against a newer schema is detected: startup reports
`ahead` and refuses, rather than failing on an unexpected column later.

---

## Startup behaviour

| State | Meaning | Production | Development |
|---|---|---|---|
| `current` | Revision matches | starts | starts |
| `uninitialized` | Empty database | refuses | refuses unless `ALLOW_PENDING_MIGRATIONS=true` |
| `unstamped` | Tables, no version | refuses | refuses unless `ALLOW_PENDING_MIGRATIONS=true` |
| `behind` | Migrations pending | refuses | refuses unless `ALLOW_PENDING_MIGRATIONS=true` |
| `ahead` | Newer than this build | refuses | refuses unless `ALLOW_PENDING_MIGRATIONS=true` |

`ALLOW_PENDING_MIGRATIONS` is a development escape hatch and is **ignored in
production**, where a schema mismatch always refuses startup.

---

## Tables

Ten, all created by the baseline revision:

`users`, `workspaces`, `documents`, `document_chunks`, `chat_sessions`,
`chat_messages`, `reports`, `connectors`, `password_reset_tokens`,
`refresh_tokens`.

Two things people expect to find here and will not:

- **No audit table.** The audit trail is written to the log stream as
  structured JSON (`app/core/audit.py`) so it can be shipped somewhere
  append-only, rather than living in the database the application can write
  to. Moving it into a table is a design decision, not a missing migration.
- **No upload-security table.** Quarantine is a directory on disk; the
  per-document security metadata (`content_hash`, `mime_type`) lives on
  `documents`.

---

# Historical manual SQL (pre-Alembic)

Everything below predates Alembic and is retained so an old database can be
brought up to the baseline before being stamped. On a database created by
`alembic upgrade head` these are already included — do not run them.

## 001 - Document content hashing and MIME type

```sql
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS mime_type VARCHAR(255);

CREATE INDEX IF NOT EXISTS ix_documents_workspace_content_hash
    ON documents (workspace_id, content_hash);
```

Rows predating this have a NULL `content_hash` and are never matched as
duplicates. Backfilling would mean re-reading every stored file and is not
needed for correctness.

## 002 - Persisted agent trace and confidence

```sql
ALTER TABLE reports ADD COLUMN IF NOT EXISTS agent_trace JSON;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION;
```

Reports predating this have a NULL `agent_trace`; the API returns an empty
`agentSteps` list for them, which is accurate — their trace was never
recorded. Do not backfill with representative values.

## 003 - Background ingestion state

```sql
ALTER TABLE documents ADD COLUMN IF NOT EXISTS progress INTEGER NOT NULL DEFAULT 0;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMP;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS processing_completed_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_documents_status ON documents (status);
```

Rows predating this keep `progress = 0` and NULL timestamps. Any document
left in the old `pending` status is treated as terminal-unknown by the API;
to have it indexed, set it to `queued` and a worker will pick it up:

```sql
UPDATE documents SET status = 'queued', progress = 0 WHERE status = 'pending';
```
