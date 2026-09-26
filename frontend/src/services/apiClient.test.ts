import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  apiRequest,
  onSessionExpired,
  refreshSession,
  tokenStore,
} from './apiClient';

describe('tokenStore', () => {
  beforeEach(() => localStorage.clear());

  it('stores and reads the access token', () => {
    tokenStore.set('access-1', 'refresh-1');
    expect(tokenStore.getAccess()).toBe('access-1');
    expect(tokenStore.getRefresh()).toBe('refresh-1');
    expect(tokenStore.hasSession()).toBe(true);
  });

  it('clears both tokens', () => {
    tokenStore.set('a', 'r');
    tokenStore.clear();
    expect(tokenStore.getAccess()).toBeNull();
    expect(tokenStore.getRefresh()).toBeNull();
    expect(tokenStore.hasSession()).toBe(false);
  });

  it('leaves the refresh token alone when it is not supplied', () => {
    tokenStore.set('a', 'r');
    tokenStore.set('b');
    expect(tokenStore.getRefresh()).toBe('r');
  });
});

describe('ApiError', () => {
  it('exposes the backend envelope', () => {
    const error = new ApiError('Validation error', {
      status: 422,
      code: 'VALIDATION_ERROR',
      requestId: 'req-1',
      details: [{ msg: 'Password must contain a number.' }, { msg: 'Too short.' }],
    });
    expect(error.status).toBe(422);
    expect(error.code).toBe('VALIDATION_ERROR');
    expect(error.fieldMessages).toEqual([
      'Password must contain a number.',
      'Too short.',
    ]);
  });

  it('reports an empty field list when there are no details', () => {
    expect(new ApiError('boom').fieldMessages).toEqual([]);
  });
});

describe('apiRequest', () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  const jsonResponse = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });

  it('returns the parsed body on success', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ok: true }));
    await expect(apiRequest('/thing')).resolves.toEqual({ ok: true });
  });

  it('attaches the bearer token when one is stored', async () => {
    tokenStore.set('token-123');
    fetchMock.mockResolvedValueOnce(jsonResponse({}));
    await apiRequest('/thing');
    const headers = fetchMock.mock.calls[0][1].headers;
    expect(headers.Authorization).toBe('Bearer token-123');
  });

  it('omits the bearer token for anonymous requests', async () => {
    tokenStore.set('token-123');
    fetchMock.mockResolvedValueOnce(jsonResponse({}));
    await apiRequest('/auth/login', { anonymous: true });
    expect(fetchMock.mock.calls[0][1].headers.Authorization).toBeUndefined();
  });

  it('builds a query string, skipping empty values', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}));
    await apiRequest('/documents', { query: { workspace_id: 'ws-1', empty: undefined } });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/documents?workspace_id=ws-1');
  });

  it('throws a typed ApiError carrying the envelope', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(
        { error: { code: 'RESOURCE_NOT_FOUND', message: 'Not found', request_id: 'r-9' } },
        404
      )
    );
    await expect(apiRequest('/missing', { retries: 0 })).rejects.toMatchObject({
      status: 404,
      code: 'RESOURCE_NOT_FOUND',
      requestId: 'r-9',
    });
  });

  it('never substitutes mock data when the request fails', async () => {
    // The behaviour this replaced: a failed request silently returned fixtures.
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: { message: 'nope' } }, 500));
    await expect(apiRequest('/thing', { retries: 0 })).rejects.toBeInstanceOf(ApiError);
  });

  it('surfaces a network failure as a typed error after retrying', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    await expect(apiRequest('/thing', { retries: 1 })).rejects.toMatchObject({
      isNetworkError: true,
      code: 'NETWORK_ERROR',
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('retries a 503 and succeeds on the second attempt', async () => {
    fetchMock
      .mockResolvedValueOnce(new Response('', { status: 503 }))
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    await expect(apiRequest('/thing', { retries: 1 })).resolves.toEqual({ ok: true });
  });

  it('refreshes once on 401 and replays the original request', async () => {
    tokenStore.set('stale-token', 'refresh-token');
    fetchMock
      .mockResolvedValueOnce(new Response('', { status: 401 }))
      .mockResolvedValueOnce(
        jsonResponse({ access_token: 'fresh-token', refresh_token: 'rotated' })
      )
      .mockResolvedValueOnce(jsonResponse({ ok: true }));

    await expect(apiRequest('/thing')).resolves.toEqual({ ok: true });
    // The rotated refresh token must replace the old one, or the next refresh
    // looks like token reuse to the backend.
    expect(tokenStore.getAccess()).toBe('fresh-token');
    expect(tokenStore.getRefresh()).toBe('rotated');
  });

  it('emits session-expired and clears tokens when the refresh fails', async () => {
    tokenStore.set('stale-token', 'bad-refresh');
    const listener = vi.fn();
    const unsubscribe = onSessionExpired(listener);

    fetchMock
      .mockResolvedValueOnce(new Response('', { status: 401 }))
      .mockResolvedValueOnce(new Response('', { status: 401 }));

    await expect(apiRequest('/thing')).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalled();
    expect(tokenStore.hasSession()).toBe(false);
    unsubscribe();
  });

  it('does not attempt a refresh when no refresh token is stored', async () => {
    const ok = await refreshSession();
    expect(ok).toBe(false);
  });
});
