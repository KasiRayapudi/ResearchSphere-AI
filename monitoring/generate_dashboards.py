#!/usr/bin/env python3
"""
Generate the Grafana dashboard JSON in monitoring/grafana/dashboards/.

Seven dashboards share panel shapes, thresholds and datasource wiring, so they
are generated rather than hand-maintained: editing one panel style by hand
across seven files is how dashboards drift apart.

    python monitoring/generate_dashboards.py

Committing the output means Grafana provisioning needs no build step.
"""

import json
from pathlib import Path

OUT = Path(__file__).parent / "grafana" / "dashboards"
# Bound directly to the uid set in grafana/provisioning/datasources so the
# dashboards resolve on first load rather than prompting for a datasource.
DS = {"type": "prometheus", "uid": "researchsphere-prometheus"}

# Every request metric is labelled with the matched route template, so these
# queries stay bounded regardless of traffic shape.
JOB = 'job="researchsphere-backend"'


def target(expr: str, legend: str = "", instant: bool = False) -> dict:
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend,
        "refId": "A",
        "instant": instant,
        "range": not instant,
    }


def _panel(kind, title, targets, grid, unit=None, description="", extra_options=None,
           thresholds=None):
    panel = {
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": grid,
        "targets": [
            {**t, "refId": chr(65 + i)} for i, t in enumerate(targets)
        ],
        "fieldConfig": {
            "defaults": {
                "unit": unit or "short",
                "color": {"mode": "palette-classic"},
                "custom": {},
            },
            "overrides": [],
        },
        "options": extra_options or {},
    }
    if thresholds:
        panel["fieldConfig"]["defaults"]["thresholds"] = {
            "mode": "absolute",
            "steps": thresholds,
        }
        panel["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    return panel


def stat(title, targets, grid, unit=None, description="", thresholds=None):
    return _panel(
        "stat", title, targets, grid, unit, description,
        {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
         "textMode": "auto", "colorMode": "value", "graphMode": "area"},
        thresholds,
    )


def timeseries(title, targets, grid, unit=None, description="", legend="bottom"):
    return _panel(
        "timeseries", title, targets, grid, unit, description,
        {"legend": {"displayMode": "list", "placement": legend, "showLegend": True},
         "tooltip": {"mode": "multi", "sort": "desc"}},
    )


def table(title, targets, grid, description=""):
    return _panel("table", title, targets, grid, None, description,
                  {"showHeader": True})


def heatmap(title, targets, grid, description=""):
    return _panel("heatmap", title, targets, grid, "s", description,
                  {"calculate": False, "yAxis": {"unit": "s"}})


GREEN_RED = [
    {"color": "green", "value": None},
    {"color": "red", "value": 1},
]
RED_GREEN = [
    {"color": "red", "value": None},
    {"color": "green", "value": 1},
]
LATENCY_STEPS = [
    {"color": "green", "value": None},
    {"color": "yellow", "value": 0.5},
    {"color": "red", "value": 2},
]


def dashboard(uid, title, description, panels, tags):
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["researchsphere", *tags],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "editable": True,
        "graphTooltip": 1,  # shared crosshair across panels
        "templating": {"list": []},
        "panels": panels,
    }


# ---------------------------------------------------------------------------
# 1. System overview
# ---------------------------------------------------------------------------
system_overview = dashboard(
    "rs-system-overview",
    "ResearchSphere - System Overview",
    "Service health at a glance: dependency state, traffic, errors and saturation.",
    [
        stat("Database", [target(f'researchsphere_dependency_up{{{JOB},dependency="database"}}')],
             {"h": 4, "w": 4, "x": 0, "y": 0}, thresholds=RED_GREEN,
             description="1 up, 0 down, -1 not configured."),
        stat("Qdrant", [target(f'researchsphere_dependency_up{{{JOB},dependency="qdrant"}}')],
             {"h": 4, "w": 4, "x": 4, "y": 0}, thresholds=RED_GREEN),
        stat("Redis", [target(f'researchsphere_dependency_up{{{JOB},dependency="redis"}}')],
             {"h": 4, "w": 4, "x": 8, "y": 0}, thresholds=RED_GREEN,
             description="-1 means Redis is not configured: rate limiting is "
                         "per-process and token revocation is disabled."),
        stat("Gemini", [target(f'researchsphere_dependency_up{{{JOB},dependency="gemini"}}')],
             {"h": 4, "w": 4, "x": 12, "y": 0}, thresholds=RED_GREEN),
        stat("Storage", [target(f'researchsphere_dependency_up{{{JOB},dependency="storage"}}')],
             {"h": 4, "w": 4, "x": 16, "y": 0}, thresholds=RED_GREEN),
        stat("Build", [target(f"researchsphere_app_info{{{JOB}}}", "{{version}} / {{environment}}")],
             {"h": 4, "w": 4, "x": 20, "y": 0}),

        timeseries("Request rate", [
            target(f"sum(rate(researchsphere_http_requests_total{{{JOB}}}[5m])) by (status_class)",
                   "{{status_class}}")],
            {"h": 8, "w": 12, "x": 0, "y": 4}, "reqps"),
        timeseries("Latency percentiles", [
            target(f"histogram_quantile(0.50, sum(rate(researchsphere_http_request_duration_seconds_bucket{{{JOB}}}[5m])) by (le))", "p50"),
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_http_request_duration_seconds_bucket{{{JOB}}}[5m])) by (le))", "p95"),
            target(f"histogram_quantile(0.99, sum(rate(researchsphere_http_request_duration_seconds_bucket{{{JOB}}}[5m])) by (le))", "p99")],
            {"h": 8, "w": 12, "x": 12, "y": 4}, "s"),

        stat("Error rate", [target(
            f'sum(rate(researchsphere_http_requests_total{{{JOB},status_class="5xx"}}[5m])) '
            f"/ clamp_min(sum(rate(researchsphere_http_requests_total{{{JOB}}}[5m])), 0.001)")],
            {"h": 4, "w": 6, "x": 0, "y": 12}, "percentunit",
            thresholds=[{"color": "green", "value": None},
                        {"color": "yellow", "value": 0.01},
                        {"color": "red", "value": 0.05}]),
        stat("Memory (RSS)", [target(f"researchsphere_process_memory_bytes{{{JOB}}}")],
             {"h": 4, "w": 6, "x": 6, "y": 12}, "bytes"),
        stat("System memory", [target(f"researchsphere_system_memory_percent{{{JOB}}}")],
             {"h": 4, "w": 6, "x": 12, "y": 12}, "percent",
             thresholds=[{"color": "green", "value": None},
                         {"color": "yellow", "value": 80},
                         {"color": "red", "value": 90}]),
        stat("Disk (uploads)", [target(f'researchsphere_disk_usage_percent{{{JOB},path="uploads"}}')],
             {"h": 4, "w": 6, "x": 18, "y": 12}, "percent",
             thresholds=[{"color": "green", "value": None},
                         {"color": "yellow", "value": 75},
                         {"color": "red", "value": 85}]),
    ],
    ["overview"],
)

# ---------------------------------------------------------------------------
# 2. API
# ---------------------------------------------------------------------------
api = dashboard(
    "rs-api",
    "ResearchSphere - API",
    "Per-route traffic, latency and errors.",
    [
        timeseries("Requests by route", [
            target(f"sum(rate(researchsphere_http_requests_total{{{JOB}}}[5m])) by (route)",
                   "{{route}}")],
            {"h": 9, "w": 12, "x": 0, "y": 0}, "reqps"),
        timeseries("p95 latency by route", [
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_http_request_duration_seconds_bucket{{{JOB}}}[5m])) by (le, route))",
                   "{{route}}")],
            {"h": 9, "w": 12, "x": 12, "y": 0}, "s"),
        timeseries("Status codes", [
            target(f"sum(rate(researchsphere_http_requests_total{{{JOB}}}[5m])) by (status)",
                   "{{status}}")],
            {"h": 8, "w": 8, "x": 0, "y": 9}, "reqps"),
        timeseries("Rate-limit rejections", [
            target(f"sum(rate(researchsphere_rate_limit_rejections_total{{{JOB}}}[5m])) by (tier, backend)",
                   "{{tier}} / {{backend}}")],
            {"h": 8, "w": 8, "x": 8, "y": 9}, "reqps",
            description="backend=local means Redis was unavailable and limits "
                        "are being counted per worker."),
        table("Slowest routes (p99)", [
            target(f"topk(10, histogram_quantile(0.99, sum(rate(researchsphere_http_request_duration_seconds_bucket{{{JOB}}}[30m])) by (le, route)))",
                   "{{route}}", instant=True)],
            {"h": 8, "w": 8, "x": 16, "y": 9}),
        heatmap("Latency distribution", [
            target(f"sum(rate(researchsphere_http_request_duration_seconds_bucket{{{JOB}}}[5m])) by (le)",
                   "{{le}}")],
            {"h": 8, "w": 24, "x": 0, "y": 17}),
    ],
    ["api"],
)

# ---------------------------------------------------------------------------
# 3. Authentication
# ---------------------------------------------------------------------------
auth = dashboard(
    "rs-auth",
    "ResearchSphere - Authentication",
    "Sign-in health and credential-attack signals.",
    [
        timeseries("Auth attempts", [
            target(f"sum(rate(researchsphere_auth_attempts_total{{{JOB}}}[5m])) by (action, outcome)",
                   "{{action}} / {{outcome}}")],
            {"h": 8, "w": 12, "x": 0, "y": 0}, "reqps"),
        timeseries("Login failures by reason", [
            target(f'sum(rate(researchsphere_auth_failures_total{{{JOB}}}[5m])) by (reason)',
                   "{{reason}}")],
            {"h": 8, "w": 12, "x": 12, "y": 0}, "reqps",
            description="A rise in unknown_email suggests enumeration; a rise "
                        "in bad_password against few accounts suggests "
                        "credential stuffing."),
        stat("Refresh-token reuse (1h)", [
            target(f"increase(researchsphere_token_refresh_reuse_total{{{JOB}}}[1h])")],
            {"h": 5, "w": 8, "x": 0, "y": 8},
            description="Any non-zero value means a refresh token was replayed "
                        "and a whole token family was revoked. Investigate.",
            thresholds=GREEN_RED),
        stat("Failed-login ratio", [
            target(f'sum(rate(researchsphere_auth_attempts_total{{{JOB},action="login",outcome="failure"}}[5m])) '
                   f'/ clamp_min(sum(rate(researchsphere_auth_attempts_total{{{JOB},action="login"}}[5m])), 0.001)')],
            {"h": 5, "w": 8, "x": 8, "y": 8}, "percentunit",
            thresholds=[{"color": "green", "value": None},
                        {"color": "yellow", "value": 0.3},
                        {"color": "red", "value": 0.6}]),
        timeseries("Authorization denials", [
            target(f"sum(rate(researchsphere_authz_denials_total{{{JOB}}}[5m])) by (reason)",
                   "{{reason}}")],
            {"h": 5, "w": 8, "x": 16, "y": 8}, "reqps",
            description="workspace_isolation denials indicate cross-tenant "
                        "access attempts."),
    ],
    ["security", "auth"],
)

# ---------------------------------------------------------------------------
# 4. Uploads
# ---------------------------------------------------------------------------
uploads = dashboard(
    "rs-uploads",
    "ResearchSphere - Uploads",
    "Ingestion throughput and the security pipeline's rejection reasons.",
    [
        timeseries("Upload outcomes", [
            target(f"sum(rate(researchsphere_uploads_total{{{JOB}}}[5m])) by (outcome)",
                   "{{outcome}}")],
            {"h": 8, "w": 12, "x": 0, "y": 0}, "reqps"),
        timeseries("Rejections by reason", [
            target(f"sum(rate(researchsphere_upload_rejections_total{{{JOB}}}[5m])) by (reason)",
                   "{{reason}}")],
            {"h": 8, "w": 12, "x": 12, "y": 0}, "reqps",
            description="executable_content and content_mismatch indicate "
                        "deliberately disguised files."),
        timeseries("Upload duration percentiles", [
            target(f"histogram_quantile(0.50, sum(rate(researchsphere_upload_duration_seconds_bucket{{{JOB}}}[5m])) by (le))", "p50"),
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_upload_duration_seconds_bucket{{{JOB}}}[5m])) by (le))", "p95")],
            {"h": 8, "w": 12, "x": 0, "y": 8}, "s",
            description="Ingestion is synchronous: extraction, chunking and "
                        "embedding all happen inside the request."),
        timeseries("Documents and chunks indexed", [
            target(f"sum(rate(researchsphere_documents_indexed_total{{{JOB}}}[15m]))", "documents"),
            target(f"sum(rate(researchsphere_document_chunks_total{{{JOB}}}[15m]))", "chunks")],
            {"h": 8, "w": 12, "x": 12, "y": 8}, "ops"),
        stat("Quarantined (24h)", [
            target(f'increase(researchsphere_uploads_total{{{JOB},outcome="quarantined"}}[24h])')],
            {"h": 4, "w": 8, "x": 0, "y": 16}, thresholds=GREEN_RED),
        stat("Failed (24h)", [
            target(f'increase(researchsphere_uploads_total{{{JOB},outcome="failed"}}[24h])')],
            {"h": 4, "w": 8, "x": 8, "y": 16}),
        stat("Duplicates ignored (24h)", [
            target(f'increase(researchsphere_uploads_total{{{JOB},outcome="duplicate"}}[24h])')],
            {"h": 4, "w": 8, "x": 16, "y": 16},
            description="Deduplication working as intended - not an error."),
    ],
    ["uploads"],
)

# ---------------------------------------------------------------------------
# 5. AI / RAG
# ---------------------------------------------------------------------------
ai = dashboard(
    "rs-ai",
    "ResearchSphere - AI and RAG",
    "Retrieval quality, model latency and agent workflow outcomes.",
    [
        timeseries("Chat outcomes", [
            target(f"sum(rate(researchsphere_chat_messages_total{{{JOB}}}[5m])) by (outcome)",
                   "{{outcome}}")],
            {"h": 8, "w": 12, "x": 0, "y": 0}, "reqps"),
        timeseries("Time to first token", [
            target(f"histogram_quantile(0.50, sum(rate(researchsphere_chat_time_to_first_token_seconds_bucket{{{JOB}}}[5m])) by (le))", "p50"),
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_chat_time_to_first_token_seconds_bucket{{{JOB}}}[5m])) by (le))", "p95")],
            {"h": 8, "w": 12, "x": 12, "y": 0}, "s",
            description="What users perceive as responsiveness. Total stream "
                        "duration hides this entirely."),
        timeseries("Retrieval latency", [
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_retrieval_duration_seconds_bucket{{{JOB}}}[5m])) by (le))", "p95")],
            {"h": 8, "w": 8, "x": 0, "y": 8}, "s"),
        timeseries("Chunks returned per search", [
            target(f"histogram_quantile(0.50, sum(rate(researchsphere_retrieval_results_bucket{{{JOB}}}[5m])) by (le))", "p50"),
            target(f"histogram_quantile(0.10, sum(rate(researchsphere_retrieval_results_bucket{{{JOB}}}[5m])) by (le))", "p10")],
            {"h": 8, "w": 8, "x": 8, "y": 8},
            description="A drift toward zero means retrieval is returning "
                        "nothing and answers are ungrounded - the single most "
                        "useful RAG health signal."),
        timeseries("Embedding latency", [
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_embedding_duration_seconds_bucket{{{JOB}}}[5m])) by (le))", "p95")],
            {"h": 8, "w": 8, "x": 16, "y": 8}, "s"),
        timeseries("Model latency by operation", [
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_llm_duration_seconds_bucket{{{JOB}}}[5m])) by (le, operation))",
                   "{{operation}}")],
            {"h": 8, "w": 12, "x": 0, "y": 16}, "s"),
        timeseries("Reports and research runs", [
            target(f"sum(rate(researchsphere_reports_generated_total{{{JOB}}}[15m])) by (outcome)",
                   "report {{outcome}}"),
            target(f"sum(rate(researchsphere_research_sessions_total{{{JOB}}}[15m])) by (outcome)",
                   "research {{outcome}}")],
            {"h": 8, "w": 12, "x": 12, "y": 16}, "ops"),
    ],
    ["ai", "rag"],
)

# ---------------------------------------------------------------------------
# 6. Performance
# ---------------------------------------------------------------------------
performance = dashboard(
    "rs-performance",
    "ResearchSphere - Performance",
    "Saturation, slow requests and dependency timings.",
    [
        timeseries("Slow requests", [
            target(f"sum(rate(researchsphere_http_slow_requests_total{{{JOB}}}[5m])) by (route)",
                   "{{route}}")],
            {"h": 8, "w": 12, "x": 0, "y": 0}, "reqps",
            description="Requests over SLOW_REQUEST_THRESHOLD_MS. Each is also "
                        "logged with its request id."),
        timeseries("In-flight requests", [
            target(f"sum(researchsphere_http_requests_in_progress{{{JOB}}})", "in flight")],
            {"h": 8, "w": 12, "x": 12, "y": 0}),
        timeseries("Database query latency", [
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_db_query_duration_seconds_bucket{{{JOB}}}[5m])) by (le, operation))",
                   "{{operation}}")],
            {"h": 8, "w": 12, "x": 0, "y": 8}, "s"),
        timeseries("Dependency probe latency", [
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_dependency_check_duration_seconds_bucket{{{JOB}}}[5m])) by (le, dependency))",
                   "{{dependency}}")],
            {"h": 8, "w": 12, "x": 12, "y": 8}, "s"),
        timeseries("Process memory", [
            target(f"researchsphere_process_memory_bytes{{{JOB}}}", "{{instance}}")],
            {"h": 8, "w": 8, "x": 0, "y": 16}, "bytes",
            description="Each uvicorn worker loads its own copy of the "
                        "embedding model, so memory scales with WEB_CONCURRENCY."),
        timeseries("CPU", [
            target(f"researchsphere_process_cpu_percent{{{JOB}}}", "{{instance}}")],
            {"h": 8, "w": 8, "x": 8, "y": 16}, "percent"),
        timeseries("Request body size", [
            target(f"histogram_quantile(0.95, sum(rate(researchsphere_http_request_size_bytes_bucket{{{JOB}}}[5m])) by (le))", "p95")],
            {"h": 8, "w": 8, "x": 16, "y": 16}, "bytes"),
    ],
    ["performance"],
)

# ---------------------------------------------------------------------------
# 7. Errors
# ---------------------------------------------------------------------------
errors = dashboard(
    "rs-errors",
    "ResearchSphere - Errors",
    "Failures by type, route and dependency.",
    [
        stat("5xx rate", [target(
            f'sum(rate(researchsphere_http_requests_total{{{JOB},status_class="5xx"}}[5m])) '
            f"/ clamp_min(sum(rate(researchsphere_http_requests_total{{{JOB}}}[5m])), 0.001)")],
            {"h": 5, "w": 6, "x": 0, "y": 0}, "percentunit",
            thresholds=[{"color": "green", "value": None},
                        {"color": "yellow", "value": 0.01},
                        {"color": "red", "value": 0.05}]),
        stat("4xx rate", [target(
            f'sum(rate(researchsphere_http_requests_total{{{JOB},status_class="4xx"}}[5m])) '
            f"/ clamp_min(sum(rate(researchsphere_http_requests_total{{{JOB}}}[5m])), 0.001)")],
            {"h": 5, "w": 6, "x": 6, "y": 0}, "percentunit"),
        stat("Unhandled exceptions (1h)", [
            target(f"sum(increase(researchsphere_http_exceptions_total{{{JOB}}}[1h]))")],
            {"h": 5, "w": 6, "x": 12, "y": 0}, thresholds=GREEN_RED),
        stat("Database errors (1h)", [
            target(f"sum(increase(researchsphere_db_errors_total{{{JOB}}}[1h]))")],
            {"h": 5, "w": 6, "x": 18, "y": 0}, thresholds=GREEN_RED),

        timeseries("5xx by route", [
            target(f'sum(rate(researchsphere_http_requests_total{{{JOB},status_class="5xx"}}[5m])) by (route)',
                   "{{route}}")],
            {"h": 8, "w": 12, "x": 0, "y": 5}, "reqps"),
        timeseries("Exceptions by type", [
            target(f"sum(rate(researchsphere_http_exceptions_total{{{JOB}}}[5m])) by (exception)",
                   "{{exception}}")],
            {"h": 8, "w": 12, "x": 12, "y": 5}, "reqps"),
        timeseries("Failed AI operations", [
            target(f'sum(rate(researchsphere_chat_messages_total{{{JOB},outcome="failed"}}[5m]))', "chat"),
            target(f'sum(rate(researchsphere_reports_generated_total{{{JOB},outcome="failed"}}[5m]))', "reports"),
            target(f'sum(rate(researchsphere_research_sessions_total{{{JOB},outcome="failed"}}[5m]))', "research")],
            {"h": 8, "w": 12, "x": 0, "y": 13}, "reqps"),
        timeseries("Redis operation failures", [
            target(f'sum(rate(researchsphere_redis_operations_total{{{JOB},outcome="failure"}}[5m])) by (operation)',
                   "{{operation}}")],
            {"h": 8, "w": 12, "x": 12, "y": 13}, "reqps",
            description="Failures mean rate limiting has degraded to "
                        "per-process counters and token revocation is off."),
    ],
    ["errors"],
)


DASHBOARDS = {
    "system-overview.json": system_overview,
    "api.json": api,
    "authentication.json": auth,
    "uploads.json": uploads,
    "ai-rag.json": ai,
    "performance.json": performance,
    "errors.json": errors,
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for filename, spec in DASHBOARDS.items():
        path = OUT / filename
        path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        panels = len(spec["panels"])
        print(f"  {filename:24} {panels:2} panels  ({spec['title']})")
    print(f"\n{len(DASHBOARDS)} dashboards written to {OUT}")


if __name__ == "__main__":
    main()
