"""
Celery application for background document ingestion.

Broker and result backend both use the Redis instance the rest of the
application already depends on, on separate logical databases so a
``FLUSHDB`` against the cache cannot destroy queued work.

Reliability settings and why each one is here:

``acks_late``
    A task is acknowledged after it finishes, not when it is delivered. If a
    worker is killed mid-ingest the broker redelivers the task instead of
    dropping it silently.

``task_reject_on_worker_lost``
    Pairs with ``acks_late``: a task whose worker died is requeued rather
    than being marked succeeded.

``worker_prefetch_multiplier = 1``
    Ingestion tasks are long and uneven. Prefetching would let one worker sit
    on several documents while another idles.

``task_time_limit`` / ``task_soft_time_limit``
    A hung extraction must not hold a slot forever. The soft limit raises
    inside the task so it can record the failure; the hard limit kills it.
"""

from celery import Celery

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("worker")

#: Redis logical databases. The cache and rate limiter use the default (0).
BROKER_DB = 1
RESULT_DB = 2


def _redis_db_url(base_url: str, db: int) -> str:
    """Point a Redis URL at a specific logical database."""
    base = base_url.rstrip("/")
    # Strip an existing database suffix if the URL already carries one.
    scheme, _, remainder = base.partition("://")
    host_part, slash, tail = remainder.partition("/")
    if slash and tail.isdigit():
        remainder = host_part
    return f"{scheme}://{remainder}/{db}"


def build_celery() -> Celery:
    """Construct the Celery app from configuration."""
    broker = _redis_db_url(settings.REDIS_URL, BROKER_DB) if settings.redis_enabled else None
    backend = _redis_db_url(settings.REDIS_URL, RESULT_DB) if settings.redis_enabled else None

    app = Celery("researchsphere", broker=broker, backend=backend)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # Reliability
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_track_started=True,
        # A document that cannot be parsed in ten minutes will not be parsed.
        task_soft_time_limit=540,
        task_time_limit=600,
        # Results are only used for operator introspection; they should not
        # accumulate in Redis indefinitely.
        result_expires=86400,
        broker_connection_retry_on_startup=True,
        # Periodic reclaim of documents whose worker died. Only runs where a
        # celery beat process is deployed; the same task can be invoked by
        # hand, and the API exposes nothing that depends on it.
        beat_schedule={
            "reap-stalled-documents": {
                "task": "documents.reap_stalled",
                "schedule": 300.0,
            },
            # Housekeeping, not a user-facing path: daily is often enough to
            # keep orphaned objects and abandoned temp files from
            # accumulating, and rare enough that listing a large bucket is
            # not a recurring cost.
            "cleanup-storage": {
                "task": "storage.cleanup",
                "schedule": 86400.0,
            },
        },
    )
    # Declared rather than autodiscovered with force=True: forcing discovery
    # here imports app.worker.tasks while this module is still initialising,
    # and tasks.py imports celery_app back. Celery resolves `imports` when the
    # worker finalises, which breaks the cycle.
    app.conf.imports = ("app.worker.tasks",)
    return app


celery_app = build_celery()
