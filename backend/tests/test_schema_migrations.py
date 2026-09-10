"""
Migrations and the startup schema gate.

The whole suite already runs against a database built by `alembic upgrade
head` (see conftest), so the baseline being correct is proved by every other
test in the project. What is tested here is the part nothing else exercises:
that the migration matches the models exactly, and that startup refuses to
run against a schema this build was not written for.
"""

import pytest
from sqlalchemy import create_engine, inspect

from app.core.schema import (
    SchemaState,
    check_schema,
    current_revision,
    head_revision,
    verify_schema,
)

pytestmark = pytest.mark.integration


def _migrated_engine(tmp_path, name="migrated.db"):
    """A throwaway database built by running the migrations."""
    from alembic import command
    from app.core.config import settings
    from app.core.schema import alembic_config

    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    original = settings.DATABASE_URL
    settings.DATABASE_URL = url
    try:
        command.upgrade(alembic_config(), "head")
    finally:
        settings.DATABASE_URL = original
    return create_engine(url)


def _model_metadata():
    """The MetaData the mapped classes are actually attached to.

    Deliberately not `app.core.database.Base.metadata`:
    tests/test_infrastructure.py reloads app.core.database to exercise the
    SQLite fallback, which rebinds Base to a fresh class with empty
    metadata. The model modules are not reloaded, so they stay mapped to the
    original. Reading the metadata off a model is correct either way.
    """
    from app.models.user import User

    return User.__table__.metadata


def _models_engine(tmp_path, name="models.db"):
    """A throwaway database built straight from the models."""
    engine = create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    _model_metadata().create_all(bind=engine)
    return engine


def _describe(engine) -> dict:
    inspector = inspect(engine)
    out = {}
    for table in sorted(inspector.get_table_names()):
        if table == "alembic_version":
            continue
        out[table] = {
            "columns": {
                c["name"]: (str(c["type"]).upper(), bool(c["nullable"]))
                for c in inspector.get_columns(table)
            },
            "pk": tuple(inspector.get_pk_constraint(table).get("constrained_columns") or ()),
            "indexes": {
                i["name"]: (tuple(i["column_names"]), bool(i.get("unique")))
                for i in inspector.get_indexes(table)
            },
        }
    return out


class TestBaselineMatchesModels:
    def test_the_migration_builds_the_schema_the_models_describe(self, tmp_path):
        """A drift here means a fresh install gets an untested schema."""
        migrated = _describe(_migrated_engine(tmp_path))
        models = _describe(_models_engine(tmp_path))
        assert migrated == models

    def test_every_model_table_is_created(self, tmp_path):
        migrated = set(_describe(_migrated_engine(tmp_path, "t.db")))
        assert migrated == set(_model_metadata().tables)
        # Guards against a model being added without a migration.
        assert len(migrated) == 10

    def test_there_is_exactly_one_head(self):
        """Two heads mean `upgrade head` is ambiguous and will fail."""
        from alembic.script import ScriptDirectory
        from app.core.schema import alembic_config

        heads = ScriptDirectory.from_config(alembic_config()).get_heads()
        assert len(heads) == 1, f"expected a single head, found {heads}"

    def test_the_migration_can_be_reversed(self, tmp_path):
        """A migration without a working downgrade cannot be rolled back."""
        from alembic import command
        from app.core.config import settings
        from app.core.schema import alembic_config

        url = f"sqlite:///{(tmp_path / 'rollback.db').as_posix()}"
        original = settings.DATABASE_URL
        settings.DATABASE_URL = url
        try:
            config = alembic_config()
            command.upgrade(config, "head")
            engine = create_engine(url)
            assert _describe(engine)  # tables exist

            command.downgrade(config, "-1")
            assert _describe(engine) == {}  # baseline removed everything

            command.upgrade(config, "head")
            assert set(_describe(engine)) == set(_describe(_models_engine(tmp_path, "cmp.db")))
            engine.dispose()
        finally:
            settings.DATABASE_URL = original


class TestSchemaCheck:
    def test_a_migrated_database_reports_current(self, tmp_path):
        status = check_schema(_migrated_engine(tmp_path, "current.db"))
        assert status.state == SchemaState.CURRENT
        assert status.current == status.head == head_revision()

    def test_an_empty_database_is_uninitialized(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}")
        status = check_schema(engine)
        assert status.state == SchemaState.UNINITIALIZED
        # The message must say what to do, not merely that something is wrong.
        assert "alembic upgrade head" in status.detail

    def test_tables_without_a_version_are_unstamped(self, tmp_path):
        """The state an existing create_all database lands in."""
        status = check_schema(_models_engine(tmp_path, "unstamped.db"))
        assert status.state == SchemaState.UNSTAMPED
        assert "alembic stamp head" in status.detail
        # And it must steer away from the destructive option.
        assert "do not run `alembic upgrade head`" in status.detail

    def test_an_unknown_revision_is_reported_as_ahead(self, tmp_path):
        """Rolling the app back without rolling the schema back."""
        from sqlalchemy import text

        engine = _migrated_engine(tmp_path, "ahead.db")
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = 'ffffffffffff'"))

        status = check_schema(engine)
        assert status.state == SchemaState.AHEAD
        assert "newer version" in status.detail

    def test_an_unreachable_database_is_unknown(self):
        engine = create_engine("postgresql://nobody:nobody@127.0.0.1:1/none")
        assert check_schema(engine).state == SchemaState.UNKNOWN

    def test_current_revision_is_none_before_stamping(self, tmp_path):
        engine = create_engine(f"sqlite:///{(tmp_path / 'none.db').as_posix()}")
        assert current_revision(engine) is None


class TestStartupGate:
    def test_a_current_schema_is_allowed(self, tmp_path):
        assert verify_schema(_migrated_engine(tmp_path, "ok.db")).is_current

    def test_production_refuses_a_pending_migration(self, tmp_path):
        from app.core.config import ConfigurationError, settings

        engine = create_engine(f"sqlite:///{(tmp_path / 'prod.db').as_posix()}")
        original_env = settings.ENVIRONMENT
        original_flag = settings.ALLOW_PENDING_MIGRATIONS
        settings.ENVIRONMENT = "production"
        # Even with the escape hatch set, production must refuse.
        settings.ALLOW_PENDING_MIGRATIONS = True
        try:
            with pytest.raises(ConfigurationError, match="Refusing to start"):
                verify_schema(engine)
        finally:
            settings.ENVIRONMENT = original_env
            settings.ALLOW_PENDING_MIGRATIONS = original_flag

    def test_development_also_refuses_by_default(self, tmp_path):
        """The escape hatch has to be asked for, not assumed."""
        from app.core.config import ConfigurationError, settings

        engine = create_engine(f"sqlite:///{(tmp_path / 'dev.db').as_posix()}")
        original = settings.ALLOW_PENDING_MIGRATIONS
        settings.ALLOW_PENDING_MIGRATIONS = False
        try:
            with pytest.raises(ConfigurationError):
                verify_schema(engine)
        finally:
            settings.ALLOW_PENDING_MIGRATIONS = original

    def test_development_may_opt_in(self, tmp_path):
        """Opting in must be loud: the warning is the only signal left."""
        import logging as _logging

        from app.core.config import settings

        engine = create_engine(f"sqlite:///{(tmp_path / 'optin.db').as_posix()}")
        captured = []

        class _Capture(_logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = _Capture()
        schema_logger = _logging.getLogger("researchsphere.schema")
        schema_logger.addHandler(handler)
        # Set explicitly rather than inheriting: pytest's logging plugin
        # adjusts levels around each test, and without this the warning is
        # filtered before it reaches the handler above.
        previous_level = schema_logger.level
        schema_logger.setLevel(_logging.WARNING)

        original = settings.ALLOW_PENDING_MIGRATIONS
        settings.ALLOW_PENDING_MIGRATIONS = True
        try:
            status = verify_schema(engine)
        finally:
            settings.ALLOW_PENDING_MIGRATIONS = original
            schema_logger.removeHandler(handler)
            schema_logger.setLevel(previous_level)

        assert status.state == SchemaState.UNINITIALIZED
        assert any("ALLOW_PENDING_MIGRATIONS" in message for message in captured), captured


class TestMigrationsDoNotBreakLogging:
    def test_application_loggers_survive_running_migrations(self):
        """Regression: env.py switched off the whole logging hierarchy.

        logging.config.fileConfig defaults to disable_existing_loggers=True,
        so calling it inside alembic/env.py disabled every researchsphere.*
        logger. The application then produced no logs at all, with nothing
        raised and nothing to notice. The suite runs migrations in-process,
        so this had disabled logging for every test that followed.
        """
        import logging

        # conftest has already run `alembic upgrade head` for this session.
        for name in (
            "researchsphere.schema",
            "researchsphere.middleware",
            "researchsphere.audit",
            "researchsphere.documents",
        ):
            logger = logging.getLogger(name)
            assert not logger.disabled, f"{name} was disabled by running migrations"
            assert logger.isEnabledFor(logging.WARNING), f"{name} cannot log warnings"


class TestNothingCreatesTablesImplicitly:
    def test_startup_does_not_call_create_all(self):
        """Regression: create_all masked schema drift instead of reporting it.

        It creates missing tables but never alters an existing one, so a
        database that had drifted looked healthy until a query hit a column
        that was never added.
        """
        import inspect as _inspect

        import main

        source = _inspect.getsource(main)
        # The call, not the word: the module explains in a comment why this
        # was removed, and that explanation should stay.
        assert "create_all(" not in source
