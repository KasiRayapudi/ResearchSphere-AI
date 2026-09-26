"""
Schema version verification.

The application used to call ``Base.metadata.create_all()`` at startup
outside production. That is convenient and quietly dangerous: create_all
creates tables that are missing but never alters one that exists, so a
database that had drifted looked healthy right up until a query hit a column
that was never added.

Nothing creates tables now. Startup checks the database against Alembic's
recorded revision and refuses to run on a schema the code was not written
for, which turns a class of runtime failures into one clear message at boot.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("schema")

#: backend/alembic.ini, from backend/app/core/schema.py.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


class SchemaState:
    """Vocabulary for the outcome of the check."""

    CURRENT = "current"
    #: The database is behind: migrations exist that have not been applied.
    BEHIND = "behind"
    #: No tables and no version table: nothing has ever been migrated here.
    UNINITIALIZED = "uninitialized"
    #: Tables exist but Alembic has never stamped them. Almost always a
    #: database built by the old create_all path.
    UNSTAMPED = "unstamped"
    #: The database is at a revision this build does not know about, which
    #: means it was migrated by newer code. Rolling back the application
    #: without rolling back the schema lands here.
    AHEAD = "ahead"
    #: The check itself could not run.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SchemaStatus:
    state: str
    current: str | None
    head: str | None
    detail: str

    @property
    def is_current(self) -> bool:
        return self.state == SchemaState.CURRENT


def alembic_config() -> Config:
    """Alembic config pointed at this installation."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def head_revision() -> str | None:
    """The newest revision this build of the code ships."""
    try:
        return ScriptDirectory.from_config(alembic_config()).get_current_head()
    except Exception as exc:
        logger.error(f"Could not read the migration scripts: {exc}")
        return None


def current_revision(engine: Engine) -> str | None:
    """The revision the database records, or None if it has never been stamped."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def check_schema(engine: Engine) -> SchemaStatus:
    """Compare the database's revision with the one this build expects."""
    head = head_revision()
    if head is None:
        return SchemaStatus(
            SchemaState.UNKNOWN, None, None, "the migration scripts could not be read"
        )

    try:
        current = current_revision(engine)
        has_tables = bool([t for t in inspect(engine).get_table_names() if t != "alembic_version"])
    except Exception as exc:
        return SchemaStatus(SchemaState.UNKNOWN, None, head, f"the database is unreachable: {exc}")

    if current == head:
        return SchemaStatus(SchemaState.CURRENT, current, head, "the schema is up to date")

    if current is None:
        if not has_tables:
            return SchemaStatus(
                SchemaState.UNINITIALIZED,
                None,
                head,
                "the database is empty. Run `alembic upgrade head` to create the schema.",
            )
        return SchemaStatus(
            SchemaState.UNSTAMPED,
            None,
            head,
            "the database has tables but no Alembic version. If this schema was "
            "created by an earlier build, run `alembic stamp head` to record it as "
            "current; do not run `alembic upgrade head`, which would try to create "
            "tables that already exist.",
        )

    # A revision is recorded but is not the head. Whether it is behind or
    # ahead depends on which one the script directory knows about.
    try:
        script = ScriptDirectory.from_config(alembic_config())
        known = {rev.revision for rev in script.walk_revisions()}
    except Exception:
        known = set()

    if current not in known:
        return SchemaStatus(
            SchemaState.AHEAD,
            current,
            head,
            f"the database is at revision {current}, which this build does not "
            "contain. It was migrated by a newer version of the application. "
            "Deploy that version, or roll the schema back before starting this one.",
        )

    return SchemaStatus(
        SchemaState.BEHIND,
        current,
        head,
        f"the database is at revision {current} but this build expects {head}. "
        "Run `alembic upgrade head`.",
    )


def verify_schema(engine: Engine) -> SchemaStatus:
    """Check the schema and decide whether the application may start.

    Raises in production for anything but a current schema. Outside
    production the same problem is reported and startup continues only when
    ALLOW_PENDING_MIGRATIONS is set, so a developer has to opt in rather
    than discovering the mismatch through a confusing query error later.
    """
    from app.core.config import ConfigurationError

    status = check_schema(engine)
    if status.is_current:
        logger.info(f"[startup] database schema at {status.current}")
        return status

    message = (
        f"Database schema check failed ({status.state}): {status.detail} "
        f"[database={status.current or 'none'}, expected={status.head or 'unknown'}]"
    )

    if settings.is_production or not settings.ALLOW_PENDING_MIGRATIONS:
        logger.error(f"[startup] {message}")
        raise ConfigurationError(f"Refusing to start: {message}")

    logger.warning(
        f"[startup] {message} Continuing because ALLOW_PENDING_MIGRATIONS is set. "
        "This is a development escape hatch and is ignored in production."
    )
    return status
