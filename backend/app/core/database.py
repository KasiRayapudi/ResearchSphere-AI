from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from app.core.config import settings

import logging
import os

logger = logging.getLogger("researchsphere.database")

try:
    engine = create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    # Test connection
    with engine.connect() as conn:
        pass
except Exception as e:
    # Falling back to SQLite is a development convenience only. In production a
    # database outage must surface loudly rather than silently swapping in an
    # empty local file that looks healthy.
    if settings.is_production:
        logger.error(f"Database connection failed and SQLite fallback is disabled: {e}")
        raise
    logger.warning(
        f"Database connection failed: {e}. Falling back to SQLite (development only)."
    )
    engine = create_engine(
        "sqlite:///./researchsphere.db",
        connect_args={"check_same_thread": False}
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
