# Schema changes

This project has no Alembic setup. `Base.metadata.create_all()` creates tables
that do not exist, but it **never alters a table that does**, so a database
created before a column was added will be missing that column and every query
touching it will fail at runtime.

Apply the statements below, in order, to any database created before the
change. They are idempotent on PostgreSQL (`IF NOT EXISTS`); on SQLite, drop
that clause and skip the ones that already applied.

Check what a database already has:

```sql
SELECT column_name FROM information_schema.columns WHERE table_name = 'reports';
```

---

## 001 - Document content hashing and MIME type

Added by the secure upload pipeline. `content_hash` backs duplicate detection;
`mime_type` records the type detected from the file's bytes, as opposed to the
extension the client claimed.

```sql
ALTER TABLE documents ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS mime_type VARCHAR(255);

-- Duplicate detection is scoped per workspace, so the index is composite.
CREATE INDEX IF NOT EXISTS ix_documents_workspace_content_hash
    ON documents (workspace_id, content_hash);
```

Rows that predate this change have a NULL `content_hash` and are simply never
matched as duplicates. Backfilling would require re-reading every stored file
and is not necessary for correctness.

---

## 002 - Persisted agent trace and confidence

The research view used to render a fixed four-step agent script with invented
task names and execution times, identical for every report. The real trace is
now recorded by the graph run and stored here.

```sql
ALTER TABLE reports ADD COLUMN IF NOT EXISTS agent_trace JSON;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS confidence_score DOUBLE PRECISION;
```

Reports generated before this change have a NULL `agent_trace`; the API returns
an empty `agentSteps` list for them, which is accurate -- their trace was never
recorded. Do not backfill with representative values.

---

## Adding the next one

1. Change the model under `app/models/`.
2. Add a numbered section here with the SQL and what happens to existing rows.
3. Note it in the commit message.

A fresh database needs none of this: `create_all()` builds the current schema.
