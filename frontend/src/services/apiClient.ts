/**
 * Core HTTP client for the ResearchSphere API.
 *
 * Responsibilities that used to be missing entirely:
 *  - Typed errors surfaced from the backend's error envelope, instead of
 *    silently substituting mock data when a request failed.
 *  - Silent access-token refresh on 401, with a single in-flight refresh
 *    shared by every concurrent request so one expiry cannot trigger a
 *    stampede of refresh calls.
 *  - Bounded retry with backoff for transient failures (429/5xx/network).
 *  - A session-expired signal the AuthContext listens for.
 */

export const API_BASE = '/api/v1';

const ACCESS_TOKEN_KEY = 'rs_auth_token';      // kept: existing key
const REFRESH_TOKEN_KEY = 'rs_refresh_token';

/** Error carrying the backend's structured envelope. */
export class ApiError extends Error {
  status: number;
  code: string;
  requestId?: string;
  details: Array<{ loc?: unknown; msg?: string; type?: string }>;
  isNetworkError: boolean;

  constructor(
    message: string,
    opts: {
      status?: number;
      code?: string;
      requestId?: string;
      details?: ApiError['details'];
      isNetworkError?: boolean;
    } = {}
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = opts.status ?? 0;
    this.code = opts.code ?? 'UNKNOWN_ERROR';
    this.requestId = opts.requestId;
    this.details = opts.details ?? [];
    this.isNetworkError = opts.isNetworkError ?? false;
  }

  /** Field-level messages, for form validation display. */
  get fieldMessages(): string[] {
    return this.details.map((d) => d.msg ?? '').filter(Boolean);
  }
}

// ---------------------------------------------------------------------------
// Token storage
// ---------------------------------------------------------------------------
export const tokenStore = {
  getAccess(): string | null {
    try {
      return localStorage.getItem(ACCESS_TOKEN_KEY);
    } catch {
      return null;
    }
  },
  getRefresh(): string | null {
    try {
      return localStorage.getItem(REFRESH_TOKEN_KEY);
    } catch {
      return null;
    }
  },
  set(access: string | null, refresh?: string | null) {
    try {
      if (access) localStorage.setItem(ACCESS_TOKEN_KEY, access);
      else localStorage.removeItem(ACCESS_TOKEN_KEY);

      if (refresh !== undefined) {
        if (refresh) localStorage.setItem(REFRESH_TOKEN_KEY, refresh);
        else localStorage.removeItem(REFRESH_TOKEN_KEY);
      }
    } catch {
      /* storage unavailable (private mode) - requests still work in-session */
    }
  },
  clear() {
    this.set(null, null);
  },
  hasSession(): boolean {
    return Boolean(this.getAccess());
  },
};

// ---------------------------------------------------------------------------
// Session-expired signal
// ---------------------------------------------------------------------------
type Listener = () => void;
const sessionExpiredListeners = new Set<Listener>();

export function onSessionExpired(fn: Listener): () => void {
  sessionExpiredListeners.add(fn);
  return () => sessionExpiredListeners.delete(fn);
}

function emitSessionExpired() {
  sessionExpiredListeners.forEach((fn) => {
    try {
      fn();
    } catch {
      /* a listener must not break the request pipeline */
    }
  });
}

// ---------------------------------------------------------------------------
// Silent refresh (single-flight)
// ---------------------------------------------------------------------------
let refreshInFlight: Promise<boolean> | null = null;

async function performRefresh(): Promise<boolean> {
  const refreshToken = tokenStore.getRefresh();
  if (!refreshToken) return false;

  try {
    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) return false;
    const data = await res.json();
    if (!data?.access_token) return false;
    // The backend rotates: the old refresh token is now dead, so the new one
    // must replace it or the next refresh will look like token reuse.
    tokenStore.set(data.access_token, data.refresh_token ?? null);
    return true;
  } catch {
    return false;
  }
}

/** Refresh the session, sharing one in-flight request across callers. */
export function refreshSession(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = performRefresh().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

// ---------------------------------------------------------------------------
// Request pipeline
// ---------------------------------------------------------------------------
export interface RequestOptions extends Omit<RequestInit, 'body'> {
  /** JSON body; ignored when `formData` is supplied. */
  json?: unknown;
  formData?: FormData;
  /** Skip the Authorization header (login, signup, password reset). */
  anonymous?: boolean;
  /** Retry attempts for transient failures. Default 2. */
  retries?: number;
  query?: Record<string, string | number | boolean | undefined | null>;
}

const RETRYABLE_STATUS = new Set([429, 502, 503, 504]);

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = `${API_BASE}${path}`;
  if (!query) return url;
  const params = new URLSearchParams();
  Object.entries(query).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') params.append(k, String(v));
  });
  const qs = params.toString();
  return qs ? `${url}?${qs}` : url;
}

async function parseError(res: Response): Promise<ApiError> {
  let payload: any = null;
  try {
    payload = await res.json();
  } catch {
    /* non-JSON error body */
  }
  const envelope = payload?.error;
  return new ApiError(
    envelope?.message || payload?.detail || res.statusText || 'Request failed',
    {
      status: res.status,
      code: envelope?.code || `HTTP_${res.status}`,
      requestId: envelope?.request_id,
      details: envelope?.details ?? [],
    }
  );
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export async function apiRequest<T>(
  path: string,
  options: RequestOptions = {}
): Promise<T> {
  const {
    json,
    formData,
    anonymous = false,
    retries = 2,
    query,
    headers: extraHeaders,
    ...rest
  } = options;

  const url = buildUrl(path, query);
  let attempt = 0;
  let refreshed = false;

   
  while (true) {
    const headers: Record<string, string> = { ...(extraHeaders as any) };
    if (!formData && json !== undefined) headers['Content-Type'] = 'application/json';
    if (!anonymous) {
      const token = tokenStore.getAccess();
      if (token) headers.Authorization = `Bearer ${token}`;
    }

    let res: Response;
    try {
      res = await fetch(url, {
        ...rest,
        headers,
        body: formData ?? (json !== undefined ? JSON.stringify(json) : undefined),
      });
    } catch (err) {
      // Network failure - retry, then surface honestly rather than pretending
      // the call succeeded.
      if (attempt < retries) {
        await sleep(2 ** attempt * 300);
        attempt += 1;
        continue;
      }
      throw new ApiError(
        'Cannot reach the server. Check your connection and try again.',
        { isNetworkError: true, code: 'NETWORK_ERROR' }
      );
    }

    // 401 -> try one silent refresh, then replay the request once.
    if (res.status === 401 && !anonymous && !refreshed) {
      refreshed = true;
      const ok = await refreshSession();
      if (ok) continue;
      tokenStore.clear();
      emitSessionExpired();
      throw await parseError(res);
    }

    if (RETRYABLE_STATUS.has(res.status) && attempt < retries) {
      const retryAfter = Number(res.headers.get('Retry-After'));
      const waitMs = Number.isFinite(retryAfter) && retryAfter > 0
        ? Math.min(retryAfter * 1000, 5000)
        : 2 ** attempt * 400;
      await sleep(waitMs);
      attempt += 1;
      continue;
    }

    if (!res.ok) throw await parseError(res);

    if (res.status === 204) return undefined as T;
    const text = await res.text();
    if (!text) return undefined as T;
    try {
      return JSON.parse(text) as T;
    } catch {
      return text as unknown as T;
    }
  }
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) =>
    apiRequest<T>(path, { ...options, method: 'GET' }),
  post: <T>(path: string, json?: unknown, options?: RequestOptions) =>
    apiRequest<T>(path, { ...options, method: 'POST', json }),
  patch: <T>(path: string, json?: unknown, options?: RequestOptions) =>
    apiRequest<T>(path, { ...options, method: 'PATCH', json }),
  delete: <T>(path: string, options?: RequestOptions) =>
    apiRequest<T>(path, { ...options, method: 'DELETE' }),
  upload: <T>(path: string, formData: FormData, options?: RequestOptions) =>
    apiRequest<T>(path, { ...options, method: 'POST', formData, retries: 0 }),
};
