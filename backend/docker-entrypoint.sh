#!/bin/sh
###############################################################################
# Container entrypoint.
#
#   validate  -> check configuration and exit (used by CI and by `docker run`)
#   serve     -> wait for dependencies, validate, then exec the server
#   <other>   -> exec whatever was passed, so `docker run ... sh` still works
#
# Every long-running command is exec'd so the server replaces this shell and
# receives SIGTERM directly, which is what makes graceful shutdown work.
###############################################################################
set -eu

log() {
    # Match the application's structured output closely enough to be greppable
    # without pulling in a JSON encoder at shell level.
    printf '{"timestamp":"%s","level":"%s","logger":"entrypoint","message":"%s"}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2"
}

# --------------------------------------------------------------------------
# Wait for a TCP dependency. Postgres and Qdrant routinely take longer to
# accept connections than the backend takes to start, and failing fast here
# produces a clearer message than a stack trace from the first query.
# --------------------------------------------------------------------------
wait_for() {
    host="$1"
    port="$2"
    name="$3"
    timeout="${4:-60}"
    waited=0

    log INFO "waiting for ${name} at ${host}:${port} (timeout ${timeout}s)"
    while [ "$waited" -lt "$timeout" ]; do
        if python -c "
import socket, sys
s = socket.socket()
s.settimeout(2)
try:
    s.connect(('${host}', ${port}))
except Exception:
    sys.exit(1)
finally:
    s.close()
" 2>/dev/null; then
            log INFO "${name} is accepting connections"
            return 0
        fi
        waited=$((waited + 2))
        sleep 2
    done

    log ERROR "${name} at ${host}:${port} did not become reachable within ${timeout}s"
    return 1
}

# Parse host/port out of a URL without needing extra tooling in the image.
url_host() { python -c "import urllib.parse,sys; print(urllib.parse.urlparse(sys.argv[1]).hostname or '')" "$1"; }
url_port() { python -c "import urllib.parse,sys; print(urllib.parse.urlparse(sys.argv[1]).port or sys.argv[2])" "$1" "$2"; }

wait_for_dependencies() {
    if [ "${SKIP_DEPENDENCY_WAIT:-false}" = "true" ]; then
        log WARN "SKIP_DEPENDENCY_WAIT=true - not waiting for dependencies"
        return 0
    fi

    if [ -n "${DATABASE_URL:-}" ]; then
        db_host="$(url_host "$DATABASE_URL")"
        db_port="$(url_port "$DATABASE_URL" 5432)"
        [ -n "$db_host" ] && wait_for "$db_host" "$db_port" "database" "${DB_WAIT_TIMEOUT:-90}"
    fi

    if [ -n "${QDRANT_HOST:-}" ]; then
        wait_for "$QDRANT_HOST" "${QDRANT_PORT:-6333}" "qdrant" "${QDRANT_WAIT_TIMEOUT:-60}"
    fi

    # Redis is optional: the application degrades to in-process rate limiting
    # and skips token revocation, so a missing Redis must not block startup.
    if [ -n "${REDIS_URL:-}" ]; then
        redis_host="$(url_host "$REDIS_URL")"
        redis_port="$(url_port "$REDIS_URL" 6379)"
        if [ -n "$redis_host" ]; then
            wait_for "$redis_host" "$redis_port" "redis" "${REDIS_WAIT_TIMEOUT:-30}" \
                || log WARN "redis unreachable - continuing with degraded rate limiting"
        fi
    fi
}

validate_config() {
    log INFO "validating configuration"
    # Exits non-zero in production when configuration is invalid, so the
    # container fails immediately and visibly rather than serving traffic in a
    # misconfigured state.
    python -m app.cli.validate_config
}

case "${1:-serve}" in
    validate)
        validate_config
        ;;

    serve)
        wait_for_dependencies
        validate_config

        workers="${WEB_CONCURRENCY:-2}"
        # Graceful shutdown window: uvicorn stops accepting new connections and
        # lets in-flight requests (including SSE chat streams) finish.
        timeout="${GRACEFUL_TIMEOUT:-30}"

        log INFO "starting uvicorn (workers=${workers}, graceful timeout=${timeout}s)"
        exec python -m uvicorn main:app \
            --host 0.0.0.0 \
            --port 8000 \
            --workers "$workers" \
            --timeout-graceful-shutdown "$timeout" \
            --proxy-headers \
            --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
            --no-access-log \
            --log-level "${UVICORN_LOG_LEVEL:-info}"
        ;;

    *)
        exec "$@"
        ;;
esac
