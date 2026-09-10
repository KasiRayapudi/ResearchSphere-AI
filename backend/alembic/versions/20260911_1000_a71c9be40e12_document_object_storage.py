"""document object storage columns

Revision ID: a71c9be40e12
Revises: f3de45f7daf0
Created: 2026-09-11 10:00:00.000000+00:00

Documents used to record an absolute filesystem path. This adds the columns
that replace it -- provider, opaque key, backend metadata -- and backfills
every existing row so no upload becomes unreadable.

The backfill is exact rather than a guess: pre-Phase-6 uploads were promoted
into UPLOAD_DIR under an opaque generated name, so the basename of the
recorded path is already the key relative to the filesystem root. Nothing is
moved, re-uploaded or rewritten on disk.

``file_path`` is kept and only made nullable. Dropping it would make a
rollback lossy, and the old column is what the downgrade reads back.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a71c9be40e12"
down_revision: str | None = "f3de45f7daf0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Rows per UPDATE round trip. Bounded so the migration does not build one
#: statement per document on a large table, nor a single unbounded one.
BATCH = 500


def _basename(path: str) -> str:
    """Last segment of a path recorded on either platform.

    Done in Python rather than SQL because the separator differs between the
    rows and there is no portable regexp across SQLite and PostgreSQL.
    """
    return (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def upgrade() -> None:
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("storage_provider", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("storage_key", sa.String(length=512), nullable=True))
        batch_op.add_column(sa.Column("storage_metadata", sa.JSON(), nullable=True))
        batch_op.create_index("ix_documents_storage_key", ["storage_key"], unique=False)
        # New rows carry a key, not a path.
        batch_op.alter_column("file_path", existing_type=sa.String(length=1000), nullable=True)

    # --- backfill ---------------------------------------------------------
    connection = op.get_bind()
    documents = sa.table(
        "documents",
        sa.column("id", sa.String),
        sa.column("file_path", sa.String),
        sa.column("storage_key", sa.String),
        sa.column("storage_provider", sa.String),
    )

    rows = connection.execute(
        sa.select(documents.c.id, documents.c.file_path).where(
            documents.c.storage_key.is_(None), documents.c.file_path.isnot(None)
        )
    ).fetchall()

    updates = [
        {"row_id": row.id, "key": _basename(row.file_path)}
        for row in rows
        if _basename(row.file_path)
    ]
    statement = (
        documents.update()
        .where(documents.c.id == sa.bindparam("row_id"))
        .values(storage_key=sa.bindparam("key"), storage_provider="filesystem")
    )
    for start in range(0, len(updates), BATCH):
        connection.execute(statement, updates[start : start + BATCH])


def downgrade() -> None:
    # file_path was never cleared, so the old column still holds what the
    # application read before this revision and nothing has to be rebuilt.
    # Restoring NOT NULL would fail on any row uploaded after the upgrade, so
    # the constraint is deliberately not reinstated here.
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index("ix_documents_storage_key")
        batch_op.drop_column("storage_metadata")
        batch_op.drop_column("storage_key")
        batch_op.drop_column("storage_provider")
