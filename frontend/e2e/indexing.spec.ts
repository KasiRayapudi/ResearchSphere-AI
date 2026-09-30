import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test } from './fixtures';

const SAMPLE = new URL('./fixtures/sample-document.txt', import.meta.url);

/**
 * Where the CI evidence step looks for what this test indexed. The document id
 * is not knowable ahead of time, so the run writes it out and the workflow
 * checks the worker log and Qdrant for that exact id.
 */
const EVIDENCE = new URL('../test-results/indexed-document.json', import.meta.url);

/**
 * How long the whole asynchronous pipeline is given.
 *
 * The work itself is small -- a few hundred bytes to extract, chunk, embed and
 * upsert -- but the worker loads the embedding model into memory on its first
 * task, which dominates. CI warms the model's on-disk cache beforehand, so
 * this is generous rather than a guess to be raised when a run goes red: if it
 * is ever hit, something in the pipeline stopped, and the evidence step says
 * where.
 */
const INDEXING_TIMEOUT_MS = 120_000;

/** The states the backend will not move on from (models/document.py). */
const TERMINAL_STATE = /^(indexed|failed)$/;

/**
 * Services this journey cannot be faked without, as global-setup found them.
 *
 * Outside CI a developer may be running part of the stack -- this machine has
 * no Redis and no Qdrant at all -- and without them the document genuinely
 * fails at the vector write, so the test is declared as not runnable rather
 * than left failing. In CI it can never skip: `CI` is set on the job, and
 * global-setup has already failed the run if any of these is unreachable.
 */
const MISSING_SERVICES = (process.env.E2E_MISSING_SERVICES ?? '').split(',').filter(Boolean);

test.describe('document indexing', () => {
  // The default per-test timeout is shorter than the pipeline budget above.
  test.setTimeout(INDEXING_TIMEOUT_MS + 60_000);

  test.skip(
    !process.env.CI && MISSING_SERVICES.length > 0,
    `the indexed journey needs ${MISSING_SERVICES.join(', ')}; CI runs the full stack`
  );

  /**
   * The asynchronous journey, end to end and unmocked:
   * browser upload -> API -> Celery over Redis -> embedding -> Qdrant -> indexed.
   *
   * Step 4 proves an upload is accepted and starts processing. This proves it
   * finishes: the assertion is the application's real terminal state and
   * nothing weaker. `queued` and `processing` are explicitly not accepted, and
   * `failed` fails the test immediately rather than burning the timeout.
   *
   * The status the browser shows arrives over the realtime channel, because
   * that is the only way this page learns of it -- nothing polls
   * (DocumentManagerPage.tsx). Asserting realtime's own semantics is a later
   * test's job; here it is simply the path the product uses.
   */
  test('an uploaded document is processed by the worker and reaches indexed', async ({
    signedInPage: page,
    workerAccount,
  }, testInfo) => {
    const marker = crypto.randomUUID();
    const fileName = `e2e-indexed-${marker.slice(0, 8)}.txt`;
    // Unique bytes per run: identical content is rejected as a duplicate by
    // SHA-256, and a duplicate never reaches the worker at all.
    const contents = `${readFileSync(SAMPLE, 'utf8')}\nIndexing run marker: ${marker}\n`;

    await page.goto('/documents');
    await expect(page.getByTestId('app-shell')).toBeVisible();

    await page.getByTestId('open-upload').click();
    await expect(page.getByTestId('upload-input')).toBeAttached();

    const startedAt = Date.now();
    await page.getByTestId('upload-input').setInputFiles({
      name: fileName,
      mimeType: 'text/plain',
      buffer: Buffer.from(contents, 'utf8'),
    });

    // Upload accepted: the queue item leaves 'queued' only once the request is
    // away, and leaves 'uploading' only once the API has stored the document.
    const queueItem = page.getByTestId('upload-item').filter({ hasText: fileName });
    await expect(queueItem).toBeVisible();
    await expect(queueItem).toHaveAttribute('data-upload-status', /uploading|indexing|done/);

    const row = page.getByTestId('document-row').filter({ hasText: fileName });
    await expect(row).toBeVisible();
    const status = row.getByTestId('document-status');

    // Wait for the pipeline to settle, then require the successful outcome.
    // Split in two so that a document which genuinely failed reports itself as
    // failed, instead of looking like a timeout.
    await expect(status).toHaveText(TERMINAL_STATE, { timeout: INDEXING_TIMEOUT_MS });
    await expect(status).toHaveText('indexed');
    const indexedAfterMs = Date.now() - startedAt;

    // Chunks are counted and stored only after the embeddings have been
    // upserted into Qdrant (worker/tasks.py), so a non-zero count here is the
    // UI reporting that the vector write happened -- not merely that a row
    // changed state.
    const chunkCell = row.getByTestId('document-chunks');
    await expect(chunkCell).not.toHaveText('0');
    const chunkCountShown = Number((await chunkCell.innerText()).trim());
    expect(chunkCountShown).toBeGreaterThan(0);

    // The upload queue is a second, independent surface: it turns 'done' only
    // on an indexed status and 'error' on a failed one (UploadQueue.tsx).
    await expect(queueItem).toHaveAttribute('data-upload-status', 'done');

    // --- corroborate against the API, and leave evidence for the CI step ----
    const { account, tokens } = workerAccount;
    const authorized = { Authorization: `Bearer ${tokens.access}` };

    // `title` is the original filename (_document_payload in api/v1/documents.py)
    // and the listing is searchable on it, so this asks for the one document.
    const listed = await page.request.get('/api/v1/documents', {
      headers: authorized,
      params: { workspace_id: account.workspaceId, search: fileName },
    });
    expect(listed.ok(), `document list failed (${listed.status()})`).toBeTruthy();
    const payload = (await listed.json()) as { items: Array<{ id: string; title: string }> };
    const document = payload.items.find((d) => d.title === fileName);
    expect(document, `no document named ${fileName} in the workspace listing`).toBeTruthy();

    const reported = await page.request.get(`/api/v1/documents/${document!.id}/status`, {
      headers: authorized,
    });
    expect(reported.ok(), `status lookup failed (${reported.status()})`).toBeTruthy();
    const state = (await reported.json()) as {
      status: string;
      chunkCount: number;
      startedAt: string | null;
      completedAt: string | null;
      error: string | null;
    };
    expect(state.status, `the API disagrees with the UI: ${JSON.stringify(state)}`).toBe('indexed');
    expect(state.chunkCount).toBeGreaterThan(0);

    const evidence = {
      documentId: document!.id,
      workspaceId: account.workspaceId,
      fileName,
      chunkCountApi: state.chunkCount,
      chunkCountUi: chunkCountShown,
      processingStartedAt: state.startedAt,
      processingCompletedAt: state.completedAt,
      uploadToIndexedMs: indexedAfterMs,
    };
    mkdirSync(dirname(fileURLToPath(EVIDENCE)), { recursive: true });
    writeFileSync(fileURLToPath(EVIDENCE), JSON.stringify(evidence, null, 2), 'utf8');
    await testInfo.attach('indexed-document', {
      body: JSON.stringify(evidence, null, 2),
      contentType: 'application/json',
    });
    console.log(`[e2e] indexed in ${indexedAfterMs}ms: ${JSON.stringify(evidence)}`);
  });
});
