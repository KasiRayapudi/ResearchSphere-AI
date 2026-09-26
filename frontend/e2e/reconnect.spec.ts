import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { request as apiRequest, type WebSocket, type WebSocketRoute } from '@playwright/test';

import { expect, test } from './fixtures';

const SAMPLE = new URL('./fixtures/sample-document.txt', import.meta.url);
const EVIDENCE = new URL('../test-results/reconnect-document.json', import.meta.url);
const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:3000';

/** Same budget as the other pipeline specs; the worker may load its model. */
const INDEXING_TIMEOUT_MS = 120_000;
/** Connection events are quick: the first retry is due 250-500 ms after a drop. */
const CONNECTION_TIMEOUT_MS = 30_000;

/** The one path this test interrupts. Vite's HMR socket is left alone. */
const APP_SOCKET_PATH = '/api/v1/ws';
const STATUS_ENDPOINT = /^\/api\/v1\/documents\/[^/]+\/status$/;
const NON_TERMINAL = new Set(['queued', 'processing']);

const MISSING_SERVICES = (process.env.E2E_MISSING_SERVICES ?? '').split(',').filter(Boolean);

interface Frame {
  at: number;
  type: string;
  seq?: number;
  id?: string;
  status?: string;
  data?: Record<string, unknown>;
}

interface AppSocket {
  ws: WebSocket;
  openedAt: number;
  closedAt?: number;
  received: Frame[];
  sent: Array<{ at: number; type: string; after?: number; epoch?: string }>;
}

function parse(raw: string | Buffer): Record<string, unknown> | null {
  try {
    return JSON.parse(typeof raw === 'string' ? raw : raw.toString('utf8')) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/**
 * Resolves when a condition over recorded state becomes true. Re-checked on
 * every recorded event rather than on a clock, and bounded, so a connection
 * that never comes back fails with a reason instead of hanging.
 */
function createSignal() {
  const waiters = new Set<() => void>();
  return {
    changed: () => waiters.forEach((check) => check()),
    until: (what: string, condition: () => boolean, timeoutMs = CONNECTION_TIMEOUT_MS) =>
      new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => {
          waiters.delete(check);
          reject(new Error(`timed out after ${timeoutMs}ms waiting for: ${what}`));
        }, timeoutMs);
        const check = () => {
          if (!condition()) return;
          clearTimeout(timer);
          waiters.delete(check);
          resolve();
        };
        waiters.add(check);
        check();
      }),
  };
}

test.describe('websocket reconnect', () => {
  test.setTimeout(INDEXING_TIMEOUT_MS + 90_000);

  test.skip(
    !process.env.CI && MISSING_SERVICES.length > 0,
    `reconnect recovery needs ${MISSING_SERVICES.join(', ')}; CI runs the full stack`
  );

  /**
   * A dropped socket must not cost the page the updates it missed.
   *
   * The application socket is interrupted mid-session -- both ends, the page
   * seeing the 1006 a browser reports for a dropped connection and the backend
   * seeing its connection end -- and kept down while a document is uploaded
   * and fully indexed. The page cannot have learned of that transition: no
   * socket, and nothing polls. Then the client's own reconnect is let through,
   * and the test requires that it resumed in the same sequence space, that the
   * server replayed what it missed and called the replay complete, and that
   * the row reached `indexed` from a replayed frame on the new socket -- with
   * no reload, no refetch and no REST read after the reconnect.
   *
   * How the socket is interrupted: Playwright's routeWebSocket, scoped to
   * /api/v1/ws, in pass-through mode. Every frame is the server's own,
   * forwarded unmodified over a real connection that authenticates with the
   * page's own bearer token and Origin. The route only decides when that
   * connection ends and when the next attempt may reach the server. It never
   * sends, alters or drops a message, and it never tells the client to
   * reconnect: the retry comes from the client's own backoff timer.
   */
  test('a dropped socket reconnects, resumes, and catches up on what it missed', async ({
    signedInPage: page,
    workerAccount,
  }, testInfo) => {
    const signal = createSignal();

    // --- control of the application socket ---------------------------------
    let outage = false;
    let endOutage!: () => void;
    const outageOver = new Promise<void>((resolve) => (endOutage = resolve));
    const attempts: Array<{ at: number; duringOutage: boolean }> = [];
    const pageSides: WebSocketRoute[] = [];
    const serverSides: WebSocketRoute[] = [];

    await page.routeWebSocket(
      (url) => url.pathname === APP_SOCKET_PATH,
      async (route) => {
        attempts.push({ at: Date.now(), duringOutage: outage });
        pageSides.push(route);
        signal.changed();
        // A retry made while the network is down is held -- as a handshake
        // would be on a dead path -- until the outage ends.
        if (outage) await outageOver;
        serverSides.push(route.connectToServer());
      }
    );

    // --- what actually crossed the network -----------------------------------
    const appSockets: AppSocket[] = [];
    let otherSocketCloses = 0;
    page.on('websocket', (ws) => {
      if (new URL(ws.url()).pathname !== APP_SOCKET_PATH) {
        ws.on('close', () => (otherSocketCloses += 1));
        return;
      }
      const record: AppSocket = { ws, openedAt: Date.now(), received: [], sent: [] };
      appSockets.push(record);
      ws.on('framereceived', (frame) => {
        const parsed = parse(frame.payload);
        if (!parsed) return;
        const data = (parsed.data ?? {}) as Record<string, unknown>;
        record.received.push({
          at: Date.now(),
          type: String(parsed.type ?? ''),
          seq: typeof parsed.seq === 'number' ? parsed.seq : undefined,
          id: typeof data.id === 'string' ? data.id : undefined,
          status: typeof data.status === 'string' ? data.status : undefined,
          data,
        });
        signal.changed();
      });
      ws.on('framesent', (frame) => {
        const parsed = parse(frame.payload);
        if (!parsed) return;
        record.sent.push({
          at: Date.now(),
          type: String(parsed.type ?? ''),
          after: typeof parsed.after === 'number' ? parsed.after : undefined,
          epoch: typeof parsed.epoch === 'string' ? parsed.epoch : undefined,
        });
        signal.changed();
      });
      ws.on('close', () => {
        record.closedAt = Date.now();
        signal.changed();
      });
      signal.changed();
    });

    const statusRequests: number[] = [];
    const listRequests: number[] = [];
    page.on('request', (request) => {
      const path = new URL(request.url()).pathname;
      if (STATUS_ENDPOINT.test(path)) statusRequests.push(Date.now());
      else if (path === '/api/v1/documents') listRequests.push(Date.now());
    });
    let documentLoads = 0;
    page.on('load', () => (documentLoads += 1));

    const receivedType = (socket: AppSocket | undefined, type: string) =>
      socket?.received.find((f) => f.type === type);

    // --- 1. a healthy connection ---------------------------------------------
    await page.goto('/documents');
    await expect(page.getByTestId('app-shell')).toBeVisible();
    await signal.until('the first application socket to be ready', () =>
      Boolean(receivedType(appSockets[0], 'connection.ready'))
    );
    const loadsAtStart = documentLoads;
    const initial = appSockets[0];
    const initialEpoch = receivedType(initial, 'connection.ready')!.data!.epoch as string;

    // --- 2. interrupt it -------------------------------------------------------
    outage = true;
    const interruptedAt = Date.now();
    // Page side first, with 1006, so the client sees what a browser reports for
    // a dropped connection; then the real connection, with a code
    // WebSocket.close() accepts. The other order would hand the page a clean
    // 1000 forwarded from the server side before the 1006 could be delivered.
    await pageSides[0].close({ code: 1006, reason: 'e2e: network interrupted' });
    await serverSides[0].close({ code: 1000, reason: 'e2e: network interrupted' });
    await signal.until('the initial socket to close', () => initial.closedAt !== undefined);

    // --- 3. the client notices, and retries on its own -----------------------
    await signal.until('the client to attempt a reconnect', () =>
      attempts.some((a) => a.duringOutage)
    );
    const reconnectAttemptAt = attempts.find((a) => a.duringOutage)!.at;

    // --- 4. a real state transition the page cannot hear about ---------------
    const marker = crypto.randomUUID();
    const fileName = `e2e-reconnect-${marker.slice(0, 8)}.txt`;
    const contents = `${readFileSync(SAMPLE, 'utf8')}\nReconnect run marker: ${marker}\n`;

    await page.getByTestId('open-upload').click();
    await expect(page.getByTestId('upload-input')).toBeAttached();
    const uploadedAt = Date.now();
    await page.getByTestId('upload-input').setInputFiles({
      name: fileName,
      mimeType: 'text/plain',
      buffer: Buffer.from(contents, 'utf8'),
    });
    const row = page.getByTestId('document-row').filter({ hasText: fileName });
    await expect(row).toBeVisible();
    const status = row.getByTestId('document-status');

    // The test's own view of the backend, on a separate request context the
    // page never sees, so it cannot count as the UI polling.
    const { account, tokens } = workerAccount;
    const backend = await apiRequest.newContext({
      baseURL,
      extraHTTPHeaders: { Authorization: `Bearer ${tokens.access}` },
    });
    try {
      const listed = await backend.get('/api/v1/documents', {
        params: { workspace_id: account.workspaceId, search: fileName },
      });
      expect(listed.ok(), `document list failed (${listed.status()})`).toBeTruthy();
      const { items } = (await listed.json()) as { items: Array<{ id: string; title: string }> };
      const documentId = items.find((d) => d.title === fileName)?.id;
      expect(documentId, `no document named ${fileName} in the workspace listing`).toBeTruthy();

      const backendStatus = async () =>
        ((await (await backend.get(`/api/v1/documents/${documentId}/status`)).json()) as {
          status: string;
          chunkCount: number;
        });

      await expect
        .poll(async () => (await backendStatus()).status, {
          message: 'the backend never finished indexing while the socket was down',
          timeout: INDEXING_TIMEOUT_MS,
        })
        .toMatch(/^(indexed|failed)$/);
      const whileDown = await backendStatus();
      expect(whileDown.status, 'the document failed to index; nothing to catch up on').toBe('indexed');

      // The page missed it: the backend is done, the row still is not.
      const rowWhileDown = (await status.innerText()).trim();
      expect(
        NON_TERMINAL.has(rowWhileDown),
        `the row showed "${rowWhileDown}" while the socket was down, so it was updated some ` +
          'other way and this test would prove nothing about recovery'
      ).toBeTruthy();
      const statusReadsWhileDown = statusRequests.length;

      // --- 5. let the held retry through ---------------------------------------
      const restoredAt = Date.now();
      outage = false;
      endOutage();
      await signal.until('a second application socket', () => appSockets.length >= 2);
      const reconnected = appSockets[1];
      await signal.until('the server to finish resuming', () =>
        Boolean(receivedType(reconnected, 'connection.resumed'))
      );

      // --- 6. the row catches up from the replay ---------------------------------
      await expect(status).toHaveText(/^(indexed|failed)$/, { timeout: CONNECTION_TIMEOUT_MS });
      await expect(status).toHaveText('indexed');
      const domIndexedAt = Date.now();

      // ======================= evidence and assertions =========================
      // The initial socket really ended, and a new one really began.
      expect(appSockets, 'exactly one reconnected application socket').toHaveLength(2);
      expect(initial.closedAt!).toBeLessThanOrEqual(reconnected.openedAt);
      expect(otherSocketCloses, 'the interruption reached a socket other than the app one').toBe(0);

      // The client detected the drop itself: its retry came from its own
      // backoff, after the close and within the first backoff window.
      expect(reconnectAttemptAt).toBeGreaterThanOrEqual(initial.closedAt!);

      // It resumed rather than starting over: same epoch, asking from where it was.
      const readyAgain = receivedType(reconnected, 'connection.ready');
      expect(readyAgain, 'the new socket was never accepted by the server').toBeTruthy();
      const resume = reconnected.sent.find((f) => f.type === 'resume');
      expect(resume, 'the reconnected client never asked to resume').toBeTruthy();
      expect(resume!.epoch).toBe(initialEpoch);
      expect(readyAgain!.data!.epoch).toBe(initialEpoch);

      // The server replayed what was missed and said that was everything.
      const resumed = receivedType(reconnected, 'connection.resumed')!.data as {
        complete: boolean;
        replayed: number;
        after: number;
        seq: number;
      };
      expect(resumed.complete, `the server reported an incomplete resume: ${JSON.stringify(resumed)}`)
        .toBe(true);
      expect(resumed.replayed).toBeGreaterThan(0);

      // This document's transition arrived on the new socket, as a replay.
      const mine = reconnected.received.filter(
        (f) => f.type === 'document.status' && f.id === documentId
      );
      const indexedFrame = mine.find((f) => f.status === 'indexed');
      expect(
        indexedFrame,
        `no document.status indexed frame for ${documentId} on the new socket: ${JSON.stringify(mine)}`
      ).toBeTruthy();
      expect(mine.some((f) => NON_TERMINAL.has(f.status ?? ''))).toBeTruthy();
      expect(indexedFrame!.seq!).toBeGreaterThan(resume!.after!);
      expect(indexedFrame!.seq!).toBeLessThanOrEqual(resumed.seq);
      // Nothing about this document reached the old socket: it was down.
      expect(initial.received.some((f) => f.id === documentId)).toBe(false);

      // The event came first; the row followed it.
      expect(indexedFrame!.at).toBeLessThanOrEqual(domIndexedAt);

      // No REST did the catching up: the one status read allowed is the
      // documented "upload returned while the socket was down, ask once"
      // (UploadQueue.tsx), made during the outage; after the reconnect, none.
      expect(
        statusReadsWhileDown,
        'UploadQueue should ask once for an upload that returned while the socket was down'
      ).toBe(1);
      expect(statusRequests.filter((at) => at >= restoredAt)).toHaveLength(0);
      expect(listRequests.filter((at) => at >= restoredAt)).toHaveLength(0);

      // The same page, never reloaded.
      expect(documentLoads - loadsAtStart, 'the page reloaded during recovery').toBe(0);

      // --- REST corroboration, after the fact --------------------------------
      const final = await backendStatus();
      expect(final.status).toBe('indexed');
      expect(final.chunkCount).toBeGreaterThan(0);

      const evidence = {
        documentId,
        workspaceId: account.workspaceId,
        fileName,
        initialSocketClosed: initial.closedAt !== undefined,
        applicationSockets: appSockets.length,
        otherSocketCloses,
        reconnectAttempts: attempts.length,
        reconnectAttemptAfterCloseMs: reconnectAttemptAt - initial.closedAt!,
        resumeSent: Boolean(resume),
        resumeEpochMatches: resume!.epoch === initialEpoch,
        resumeAfter: resume!.after,
        resumedComplete: resumed.complete,
        resumedReplayed: resumed.replayed,
        resumedSeq: resumed.seq,
        documentStatusFramesOnNewSocket: mine.length,
        indexedFrameOnNewSocket: Boolean(indexedFrame),
        indexedFrameSeq: indexedFrame!.seq,
        backendStatusWhileDown: whileDown.status,
        rowStatusWhileDown: rowWhileDown,
        statusReadsWhileDown,
        statusReadsAfterReconnect: statusRequests.filter((at) => at >= restoredAt).length,
        listRequestsAfterReconnect: listRequests.filter((at) => at >= restoredAt).length,
        documentLoadsDuringTest: documentLoads - loadsAtStart,
        uploadToBackendIndexedWhileDownMs: restoredAt - uploadedAt,
        restoreToIndexedFrameMs: indexedFrame!.at - restoredAt,
        frameToDomMs: domIndexedAt - indexedFrame!.at,
        interruptionToDomIndexedMs: domIndexedAt - interruptedAt,
        restoreToDomIndexedMs: domIndexedAt - restoredAt,
        restStatus: final.status,
        restChunkCount: final.chunkCount,
      };
      mkdirSync(dirname(fileURLToPath(EVIDENCE)), { recursive: true });
      writeFileSync(fileURLToPath(EVIDENCE), JSON.stringify(evidence, null, 2), 'utf8');
      await testInfo.attach('reconnect-document', {
        body: JSON.stringify(evidence, null, 2),
        contentType: 'application/json',
      });
      console.log(`[e2e] reconnect: ${JSON.stringify(evidence)}`);
    } finally {
      await backend.dispose();
    }
  });
});
