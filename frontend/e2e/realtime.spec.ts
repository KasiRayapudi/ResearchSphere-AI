import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from './fixtures';

const SAMPLE = new URL('./fixtures/sample-document.txt', import.meta.url);
const EVIDENCE = new URL('../test-results/realtime-document.json', import.meta.url);

/** Same budget as the indexed journey: the worker's first task loads the model. */
const INDEXING_TIMEOUT_MS = 120_000;

/** Server event name; mirrors EVENTS.DOCUMENT_STATUS in services/realtime.ts. */
const DOCUMENT_STATUS = 'document.status';

/** The status vocabulary a document passes through (models/document.py). */
const NON_TERMINAL = new Set(['queued', 'processing']);

/** The one REST endpoint that could stand in for the socket (api.ts:307). */
const STATUS_ENDPOINT = /^\/api\/v1\/documents\/[^/]+\/status$/;

/** As global-setup found them; the realtime path needs the worker and its broker. */
const MISSING_SERVICES = (process.env.E2E_MISSING_SERVICES ?? '').split(',').filter(Boolean);

interface CapturedFrame {
  at: number;
  type: string;
  id?: string;
  status?: string;
  progress?: number;
  seq?: number;
}

test.describe('realtime document updates', () => {
  test.setTimeout(INDEXING_TIMEOUT_MS + 60_000);

  test.skip(
    !process.env.CI && MISSING_SERVICES.length > 0,
    `the realtime path needs ${MISSING_SERVICES.join(', ')}; CI runs the full stack`
  );

  /**
   * Prove the browser is updated *by the event*, not by asking again.
   *
   * Step 5 proves the pipeline reaches `indexed` and that Qdrant holds the
   * vectors. What it cannot distinguish is how the page found out. This reads
   * the frames of the application's own WebSocket -- no fake events, no route
   * interception, no dispatching into React -- and requires that a
   * `document.status` frame carrying this exact document's transition to
   * `indexed` arrived *before* the row changed, while the one REST endpoint
   * that could have produced the same result was not being polled.
   */
  test('the document row reaches indexed from the realtime event, not polling', async ({
    signedInPage: page,
    workerAccount,
  }, testInfo) => {
    const marker = crypto.randomUUID();
    const fileName = `e2e-realtime-${marker.slice(0, 8)}.txt`;
    const contents = `${readFileSync(SAMPLE, 'utf8')}\nRealtime run marker: ${marker}\n`;

    // Listeners go on before anything navigates: the socket is opened when the
    // app mounts, and the first frames matter.
    const socketUrls: string[] = [];
    const frames: CapturedFrame[] = [];
    page.on('websocket', (ws) => {
      socketUrls.push(ws.url());
      ws.on('framereceived', (frame) => {
        const raw =
          typeof frame.payload === 'string' ? frame.payload : frame.payload.toString('utf8');
        try {
          const parsed = JSON.parse(raw) as {
            type?: string;
            seq?: number;
            data?: { id?: string; status?: string; progress?: number };
          };
          frames.push({
            at: Date.now(),
            type: parsed.type ?? '',
            seq: parsed.seq,
            id: parsed.data?.id,
            status: parsed.data?.status,
            progress: parsed.data?.progress,
          });
        } catch {
          // Control frames the client sends/receives that are not JSON events.
        }
      });
    });

    // Only the endpoint the UI would poll with. Timestamped, so requests made
    // while waiting for the transition can be told apart from this test's own
    // corroboration afterwards.
    const statusRequests: number[] = [];
    const listRequests: number[] = [];
    page.on('request', (request) => {
      const path = new URL(request.url()).pathname;
      if (STATUS_ENDPOINT.test(path)) statusRequests.push(Date.now());
      else if (path === '/api/v1/documents') listRequests.push(Date.now());
    });

    await page.goto('/documents');
    await expect(page.getByTestId('app-shell')).toBeVisible();

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

    // The row's first rendered state, recorded rather than asserted: a fast
    // worker can finish before this reads, and the frames below carry the same
    // fact without the race.
    const firstRenderedStatus = (await status.innerText()).trim();

    await expect(status).toHaveText(/^(indexed|failed)$/, { timeout: INDEXING_TIMEOUT_MS });
    await expect(status).toHaveText('indexed');
    const domIndexedAt = Date.now();

    // --- which document was this? ------------------------------------------
    const { account, tokens } = workerAccount;
    const authorized = { Authorization: `Bearer ${tokens.access}` };
    const listed = await page.request.get('/api/v1/documents', {
      headers: authorized,
      params: { workspace_id: account.workspaceId, search: fileName },
    });
    expect(listed.ok(), `document list failed (${listed.status()})`).toBeTruthy();
    const { items } = (await listed.json()) as { items: Array<{ id: string; title: string }> };
    const document = items.find((d) => d.title === fileName);
    expect(document, `no document named ${fileName} in the workspace listing`).toBeTruthy();
    const documentId = document!.id;

    // --- the socket, and the frames that belong to this document -----------
    const appSockets = socketUrls.filter((url) => url.includes('/api/v1/ws'));
    expect(appSockets.length, `no application WebSocket was opened; saw ${JSON.stringify(socketUrls)}`)
      .toBeGreaterThan(0);
    expect(
      appSockets.some((url) => url.includes(encodeURIComponent(account.workspaceId))),
      `no socket for workspace ${account.workspaceId}; saw ${JSON.stringify(appSockets)}`
    ).toBeTruthy();

    // Only this document's events count: another test's document, another
    // workspace's, or a stale frame must not satisfy any of this.
    const mine = frames.filter((f) => f.type === DOCUMENT_STATUS && f.id === documentId);
    const indexedFrame = mine.find((f) => f.status === 'indexed');
    const progressFrames = mine.filter((f) => NON_TERMINAL.has(f.status ?? ''));

    expect(
      indexedFrame,
      `no ${DOCUMENT_STATUS} frame for ${documentId} reached the browser. ` +
        `Frames for it: ${JSON.stringify(mine)}`
    ).toBeTruthy();
    expect(
      progressFrames.length,
      `the browser never saw ${documentId} in a non-terminal state, so no transition was ` +
        `observed: ${JSON.stringify(mine)}`
    ).toBeGreaterThan(0);

    // The event must precede the DOM, or the row was updated by something else.
    expect(
      indexedFrame!.at,
      `the indexed frame arrived after the DOM already showed it (frame ${indexedFrame!.at}, ` +
        `DOM ${domIndexedAt}), so the row did not come from the event`
    ).toBeLessThanOrEqual(domIndexedAt);
    expect(progressFrames[0].at).toBeLessThanOrEqual(indexedFrame!.at);

    // --- polling ruled out --------------------------------------------------
    // Requests made while waiting for the transition. UploadQueue makes at most
    // one REST read per indexing document, and only after a resync the server
    // reported incomplete (UploadQueue.tsx). Anything more is a poll.
    const statusWhileWaiting = statusRequests.filter((at) => at <= domIndexedAt).length;
    expect(
      statusWhileWaiting,
      `the UI called the document status endpoint ${statusWhileWaiting} times before reaching ` +
        'indexed; at most one reconciliation read is expected and repeated reads are polling'
    ).toBeLessThanOrEqual(1);

    const listsWhileWaiting = listRequests.filter((at) => at > uploadedAt && at <= domIndexedAt).length;

    // --- REST corroboration, after the fact and never the cause -------------
    const reported = await page.request.get(`/api/v1/documents/${documentId}/status`, {
      headers: authorized,
    });
    expect(reported.ok(), `status lookup failed (${reported.status()})`).toBeTruthy();
    const state = (await reported.json()) as { status: string; chunkCount: number };
    expect(state.status).toBe('indexed');

    const evidence = {
      documentId,
      workspaceId: account.workspaceId,
      fileName,
      socketUrl: appSockets[0],
      firstRenderedStatus,
      statusFramesForDocument: mine.length,
      nonTerminalFrames: progressFrames.length,
      indexedFrameStatus: indexedFrame!.status,
      indexedFrameSeq: indexedFrame!.seq,
      uploadToIndexedFrameMs: indexedFrame!.at - uploadedAt,
      frameToDomMs: domIndexedAt - indexedFrame!.at,
      uploadToDomMs: domIndexedAt - uploadedAt,
      statusEndpointRequestsWhileWaiting: statusWhileWaiting,
      documentListRequestsWhileWaiting: listsWhileWaiting,
      restCorroboratedStatus: state.status,
      restChunkCount: state.chunkCount,
    };
    mkdirSync(dirname(fileURLToPath(EVIDENCE)), { recursive: true });
    writeFileSync(fileURLToPath(EVIDENCE), JSON.stringify(evidence, null, 2), 'utf8');
    await testInfo.attach('realtime-document', {
      body: JSON.stringify(evidence, null, 2),
      contentType: 'application/json',
    });
    console.log(`[e2e] realtime: ${JSON.stringify(evidence)}`);
  });
});
