###############################################################################
# ResearchSphere AI - shared application routing.
#
# Included by whichever edge server block is serving the application, so there
# is exactly one definition of every route. nginx resolves `include` at parse
# time, so these locations behave exactly as if written inline.
#
# Not named *.conf on purpose: nginx.conf includes /etc/nginx/conf.d/*.conf,
# and a bare `location` outside a `server` block is a configuration error, so
# this file must never be loaded on its own.
#
# The upstreams and the shared proxy_set_header block live in nginx.conf, at
# http level. The ACME challenge location is deliberately absent: it belongs
# only to the plain-HTTP server, which must answer it ahead of any redirect.
###############################################################################

# --- Edge health ------------------------------------------------------------
# Answered by nginx itself, so it reports the edge is up even when an upstream
# is not. The container healthcheck probes this over plain HTTP.
location = /healthz {
    access_log off;
    add_header Content-Type text/plain;
    return 200 'ok';
}

# --- Chat streaming ---------------------------------------------------------
# Must come before the generic /api/ block. Server-sent events break under
# nginx's default response buffering: the whole stream is held until the
# response completes, so tokens arrive in one burst at the end instead of
# incrementally.
#
# The shared proxy_set_header block in nginx.conf is repeated here for the same
# reason it is repeated in the WebSocket location: nginx inherits
# proxy_set_header from the enclosing level only when the current level sets
# none at all. Clearing Connection -- which is what keeps the keepalive
# upstream from seeing the client's own Connection header -- counts as setting
# one, so without these lines this location silently lost Host and the whole
# X-Forwarded-* set. Losing Host is the dangerous one: the backend would see
# the upstream name and its TrustedHost middleware would reject the request.
location /api/v1/chat/stream {
    proxy_pass http://backend_upstream;
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;
    proxy_set_header Connection '';
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Request-ID $request_id;
    # Generation can legitimately run for minutes.
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}

# --- Realtime WebSocket -----------------------------------------------------
# The application's only socket: /api/v1/ws?workspace_id=<id>, opened with the
# ["bearer", token] subprotocol (frontend/src/services/realtime.ts). The client
# derives ws: or wss: from the page's own scheme, so this one route serves both
# transports without any change here.
#
# `^~` rather than a plain prefix: a prefix match already beats /api/ on
# length, but `^~` also stops any regex location -- present or future -- from
# claiming the upgrade request and quietly proxying it without the Upgrade
# header.
#
# Every proxy_set_header below the shared block has to be repeated here: nginx
# inherits proxy_set_header from the enclosing level only when the current
# level sets none at all, so declaring Upgrade would otherwise drop Host and
# the X-Forwarded-* set. Host matters in particular -- without it the backend's
# TrustedHost middleware sees the upstream name and rejects the connection.
# Origin needs no directive: it is an ordinary request header that nginx
# forwards, and the backend checks it against CORS_ORIGINS.
location ^~ /api/v1/ws {
    proxy_pass http://backend_upstream;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Request-ID $request_id;
    # A socket carries nothing to buffer, and buffering would delay every
    # event by however long nginx decided to wait.
    proxy_buffering off;
    proxy_cache off;
    # Comfortably beyond the 25s application heartbeat
    # (WS_HEARTBEAT_INTERVAL_SECONDS), so an idle-but-healthy socket is never
    # cut by the proxy; the application's own 60s heartbeat timeout is what
    # decides a socket is dead.
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}

# --- Authentication ---------------------------------------------------------
# Tighter edge limit; the backend applies its own per-account limits too.
location ~ ^/api/v1/auth/(login|signup|refresh|password-reset) {
    limit_req zone=auth_limit burst=10 nodelay;
    limit_conn conn_limit 20;
    proxy_pass http://backend_upstream;
    proxy_read_timeout 30s;
}

# --- Uploads ----------------------------------------------------------------
# Ingestion (extract, chunk, embed, upsert) is synchronous server-side and can
# exceed the default 60s proxy timeout on a large PDF.
location /api/v1/documents/upload {
    limit_req zone=api_limit burst=20 nodelay;
    proxy_pass http://backend_upstream;
    proxy_request_buffering off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    client_max_body_size 64m;
}

# --- Long-running agent workflows ------------------------------------------
location ~ ^/api/v1/(reports/generate|research/start) {
    proxy_pass http://backend_upstream;
    proxy_read_timeout 600s;
    proxy_send_timeout 600s;
}

# --- API --------------------------------------------------------------------
location /api/ {
    limit_req zone=api_limit burst=50 nodelay;
    limit_conn conn_limit 50;
    proxy_pass http://backend_upstream;
    proxy_read_timeout 60s;
}

# --- API documentation ------------------------------------------------------
# Reachable but rate limited. Set DOCS_INTERNAL_ONLY by replacing this with a
# deny/allow pair if the schema should not be public.
location ~ ^/(docs|redoc|openapi\.json) {
    limit_req zone=api_limit burst=10 nodelay;
    proxy_pass http://backend_upstream;
}

# --- Frontend ---------------------------------------------------------------
location / {
    proxy_pass http://frontend_upstream;
    proxy_read_timeout 30s;
}
