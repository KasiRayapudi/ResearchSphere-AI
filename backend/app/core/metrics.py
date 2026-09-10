"""
Prometheus metrics for ResearchSphere AI.

Two things drive the design here:

*Cardinality.* Labels use the matched **route template**
(``/api/v1/documents/{id}``), never the raw path. Labelling by raw path would
create a new time series for every document id and make the metrics store
unusable within days. Unmatched paths collapse to ``<unmatched>`` for the same
reason.

*Multiple workers.* The production entrypoint runs uvicorn with several
workers, each in its own process with its own registry. Scraping one of them
would report a fraction of the traffic. When ``PROMETHEUS_MULTIPROC_DIR`` is
set, the collector aggregates across all workers; the helpers below handle both
cases so a single-process dev server still works unchanged.
"""

import os
import time
from collections.abc import Callable
from contextlib import contextmanager

from prometheus_client import CONTENT_TYPE_LATEST as PROMETHEUS_CONTENT_TYPE
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

from app.core.logging import get_logger

logger = get_logger("metrics")

CONTENT_TYPE = PROMETHEUS_CONTENT_TYPE

#: Buckets tuned to this application rather than the library defaults: most
#: API calls are single-digit milliseconds, while RAG and agent workflows run
#: for seconds to minutes, so the tail needs real resolution.
LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
)
#: Model calls are slower still and never sub-100ms.
MODEL_LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0)
#: Uploads: bytes, spanning a 1 KB note to the 50 MB limit.
SIZE_BUCKETS = (1024, 10240, 102400, 1048576, 5242880, 10485760, 26214400, 52428800)


# ---------------------------------------------------------------- HTTP ------
http_requests_total = Counter(
    "researchsphere_http_requests_total",
    "Total HTTP requests.",
    ["method", "route", "status_class", "status"],
)

http_request_duration_seconds = Histogram(
    "researchsphere_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=LATENCY_BUCKETS,
)

http_requests_in_progress = Gauge(
    "researchsphere_http_requests_in_progress",
    "Requests currently being served.",
    # Method only. The matched route is not known until the request has been
    # routed, which happens inside call_next, and labelling by raw path would
    # make this gauge unbounded.
    ["method"],
    multiprocess_mode="livesum",
)

http_request_size_bytes = Histogram(
    "researchsphere_http_request_size_bytes",
    "Request body size.",
    ["method", "route"],
    buckets=SIZE_BUCKETS,
)

http_exceptions_total = Counter(
    "researchsphere_http_exceptions_total",
    "Requests that raised an unhandled exception.",
    ["method", "route", "exception"],
)

http_slow_requests_total = Counter(
    "researchsphere_http_slow_requests_total",
    "Requests exceeding the slow-request threshold.",
    ["method", "route"],
)

rate_limit_rejections_total = Counter(
    "researchsphere_rate_limit_rejections_total",
    "Requests rejected by the rate limiter.",
    ["tier", "backend"],
)


# ------------------------------------------------------ authentication ------
auth_attempts_total = Counter(
    "researchsphere_auth_attempts_total",
    "Authentication attempts by outcome.",
    ["action", "outcome"],  # action: login|signup|refresh|logout|password_reset
)

auth_failures_total = Counter(
    "researchsphere_auth_failures_total",
    "Authentication failures by reason.",
    ["action", "reason"],
)

authz_denials_total = Counter(
    "researchsphere_authz_denials_total",
    "Authorization denials.",
    ["reason"],
)

token_refresh_reuse_total = Counter(
    "researchsphere_token_refresh_reuse_total",
    "Refresh-token reuse detections. Any non-zero value indicates a replayed "
    "or stolen token and revokes a whole token family.",
)

active_sessions = Gauge(
    "researchsphere_active_refresh_tokens",
    "Refresh tokens currently valid.",
    multiprocess_mode="liveall",
)


# ------------------------------------------------------------- uploads ------
uploads_total = Counter(
    "researchsphere_uploads_total",
    "Upload attempts by outcome.",
    ["outcome"],  # accepted|duplicate|rejected|failed|quarantined
)

upload_rejections_total = Counter(
    "researchsphere_upload_rejections_total",
    "Uploads rejected by the security pipeline, by reason.",
    ["reason"],
)

upload_duration_seconds = Histogram(
    "researchsphere_upload_duration_seconds",
    "End-to-end upload handling, including extraction, chunking and embedding.",
    buckets=MODEL_LATENCY_BUCKETS,
)

upload_size_bytes = Histogram(
    "researchsphere_upload_size_bytes",
    "Accepted upload size.",
    ["file_type"],
    buckets=SIZE_BUCKETS,
)

documents_indexed_total = Counter(
    "researchsphere_documents_indexed_total",
    "Documents successfully indexed.",
)

document_chunks_total = Counter(
    "researchsphere_document_chunks_total",
    "Chunks written to the vector store.",
)


# ------------------------------------------------------------ AI / RAG ------
chat_messages_total = Counter(
    "researchsphere_chat_messages_total",
    "Chat messages by outcome.",
    ["outcome"],  # completed|failed|stopped
)

chat_stream_duration_seconds = Histogram(
    "researchsphere_chat_stream_duration_seconds",
    "Time from request to end of stream.",
    buckets=MODEL_LATENCY_BUCKETS,
)

chat_time_to_first_token_seconds = Histogram(
    "researchsphere_chat_time_to_first_token_seconds",
    "Latency to the first streamed token - what a user actually perceives as "
    "responsiveness, which total duration hides.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 30.0),
)

llm_requests_total = Counter(
    "researchsphere_llm_requests_total",
    "Model invocations by outcome.",
    ["model", "operation", "outcome"],
)

llm_duration_seconds = Histogram(
    "researchsphere_llm_duration_seconds",
    "Model call latency.",
    ["model", "operation"],
    buckets=MODEL_LATENCY_BUCKETS,
)

llm_tokens_total = Counter(
    "researchsphere_llm_tokens_total",
    "Tokens consumed, when the provider reports them.",
    ["model", "kind"],  # kind: prompt|completion
)

embedding_duration_seconds = Histogram(
    "researchsphere_embedding_duration_seconds",
    "Embedding generation latency.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

embedding_batch_size = Histogram(
    "researchsphere_embedding_batch_size",
    "Texts embedded per call.",
    buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000),
)

retrieval_duration_seconds = Histogram(
    "researchsphere_retrieval_duration_seconds",
    "Vector search latency.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

retrieval_results = Histogram(
    "researchsphere_retrieval_results",
    "Chunks returned per search. A spike at zero means retrieval is finding "
    "nothing and answers are ungrounded.",
    buckets=(0, 1, 2, 3, 5, 8, 10, 20),
)

reports_generated_total = Counter(
    "researchsphere_reports_generated_total",
    "Report generations by outcome.",
    ["outcome"],
)

research_sessions_total = Counter(
    "researchsphere_research_sessions_total",
    "LangGraph research runs by outcome.",
    ["outcome"],
)


# ------------------------------------------------- infrastructure deps ------
db_query_duration_seconds = Histogram(
    "researchsphere_db_query_duration_seconds",
    "Database operation latency.",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

db_errors_total = Counter(
    "researchsphere_db_errors_total",
    "Database operations that raised.",
    ["operation"],
)

dependency_up = Gauge(
    "researchsphere_dependency_up",
    "Dependency reachability: 1 up, 0 down, -1 not configured.",
    ["dependency"],
    multiprocess_mode="liveall",
)

dependency_check_duration_seconds = Histogram(
    "researchsphere_dependency_check_duration_seconds",
    "Health-check probe latency per dependency.",
    ["dependency"],
    buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1.0, 2.5, 5.0, 10.0),
)

redis_operations_total = Counter(
    "researchsphere_redis_operations_total",
    "Redis operations by outcome. Failures here mean rate limiting has "
    "degraded to per-process counters and token revocation is disabled.",
    ["operation", "outcome"],
)

cache_operations_total = Counter(
    "researchsphere_cache_operations_total",
    "Cache lookups by result.",
    ["cache", "result"],  # result: hit|miss
)


# ------------------------------------------------------------ workspace -----
workspaces_total = Gauge(
    "researchsphere_workspaces_total",
    "Workspaces present.",
    multiprocess_mode="liveall",
)

users_total = Gauge(
    "researchsphere_users_total",
    "User accounts by state.",
    ["state"],
    multiprocess_mode="liveall",
)

documents_total = Gauge(
    "researchsphere_documents_total",
    "Documents by indexing status.",
    ["status"],
    multiprocess_mode="liveall",
)

storage_bytes = Gauge(
    "researchsphere_storage_bytes",
    "Bytes of uploaded documents recorded in the database.",
    multiprocess_mode="liveall",
)


# -------------------------------------------------------------- process -----
process_memory_bytes = Gauge(
    "researchsphere_process_memory_bytes",
    "Resident set size of this worker.",
    multiprocess_mode="liveall",
)

process_cpu_percent = Gauge(
    "researchsphere_process_cpu_percent",
    "CPU utilisation of this worker.",
    multiprocess_mode="liveall",
)

system_memory_percent = Gauge(
    "researchsphere_system_memory_percent",
    "Host memory utilisation.",
    multiprocess_mode="liveall",
)

disk_usage_percent = Gauge(
    "researchsphere_disk_usage_percent",
    "Disk utilisation of the upload volume.",
    ["path"],
    multiprocess_mode="liveall",
)

app_info = Gauge(
    "researchsphere_app_info",
    "Build information; the value is always 1 and the labels carry the data.",
    ["version", "environment"],
    multiprocess_mode="liveall",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_multiprocess() -> bool:
    return bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))


def render() -> bytes:
    """Render the exposition payload.

    Under multiple uvicorn workers each process keeps its own registry, so the
    values are aggregated from the shared directory instead of reporting only
    the worker that happened to serve the scrape.
    """
    if is_multiprocess():
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry)
    return generate_latest()


def observe_dependency(name: str, status: str, duration: float | None = None) -> None:
    """Record a health-check result using the status vocabulary from main.py."""
    if status in ("ok", "configured", "created"):
        value = 1.0
    elif status in ("disabled", "unavailable", "missing"):
        value = -1.0  # not configured, distinct from "down"
    else:
        value = 0.0
    dependency_up.labels(dependency=name).set(value)
    if duration is not None:
        dependency_check_duration_seconds.labels(dependency=name).observe(duration)


@contextmanager
def track_duration(histogram, **labels):
    """Time a block into ``histogram``, recording even when it raises."""
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        (histogram.labels(**labels) if labels else histogram).observe(elapsed)


@contextmanager
def track_db(operation: str):
    """Time a database operation and count failures."""
    started = time.perf_counter()
    try:
        yield
    except Exception:
        db_errors_total.labels(operation=operation).inc()
        raise
    finally:
        db_query_duration_seconds.labels(operation=operation).observe(time.perf_counter() - started)


def safe(fn: Callable, *args, **kwargs) -> None:
    """Run a metrics update, swallowing failures.

    Instrumentation must never be the reason a request fails.
    """
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"metrics update failed: {exc}")


def collect_process_metrics() -> dict:
    """Sample process and host resource usage.

    Returns the sampled values so the health endpoints can reuse them instead
    of taking a second, inconsistent reading.
    """
    sample: dict = {}
    try:
        import psutil

        process = psutil.Process()
        rss = process.memory_info().rss
        process_memory_bytes.set(rss)
        sample["process_memory_bytes"] = rss

        # interval=None returns usage since the previous call, which is what
        # a periodically-scraped gauge wants; a blocking interval would stall
        # the scrape.
        cpu = process.cpu_percent(interval=None)
        process_cpu_percent.set(cpu)
        sample["process_cpu_percent"] = cpu

        virtual = psutil.virtual_memory()
        system_memory_percent.set(virtual.percent)
        sample["system_memory_percent"] = virtual.percent
        sample["system_memory_available_bytes"] = virtual.available

        from app.core.config import settings

        usage = psutil.disk_usage(settings.UPLOAD_DIR)
        disk_usage_percent.labels(path="uploads").set(usage.percent)
        sample["disk_usage_percent"] = usage.percent
        sample["disk_free_bytes"] = usage.free
    except Exception as exc:  # pragma: no cover - psutil optional at runtime
        logger.debug(f"process metrics unavailable: {exc}")
    return sample


def set_app_info() -> None:
    from app.core.config import settings

    app_info.labels(version=settings.APP_VERSION, environment=settings.ENVIRONMENT).set(1)
