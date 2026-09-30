import { readFileSync } from 'node:fs';

import { expect, test } from './fixtures';

// The package is an ES module, so resolve the fixture from this file's URL.
const SAMPLE = new URL('./fixtures/sample-document.txt', import.meta.url);

/**
 * One upload, through the browser, against the real upload pipeline.
 *
 * The file is genuinely sent to POST /documents/upload, which validates the
 * extension, streams it into quarantine, scans it, promotes it into storage
 * and hands it to a worker. Nothing here is stubbed.
 *
 * Reaching `indexed` needs Redis, a Celery worker and Qdrant, so that
 * assertion belongs to the full-stack test that runs in CI. What this test
 * proves is that the upload is accepted and that processing has begun.
 */
test('an uploaded document appears in the library and starts processing', async ({
  signedInPage: page,
}) => {
  const marker = crypto.randomUUID();
  const fileName = `e2e-upload-${marker.slice(0, 8)}.txt`;
  // Unique content per run: identical bytes are rejected as a duplicate, by
  // SHA-256, which is exactly what the pipeline should do.
  const contents = `${readFileSync(SAMPLE, 'utf8')}\nRun marker: ${marker}\n`;

  await page.goto('/documents');
  await expect(page.getByTestId('app-shell')).toBeVisible();

  // The upload queue lives in a modal, so open it the way a user does.
  await page.getByTestId('open-upload').click();
  await expect(page.getByTestId('upload-input')).toBeAttached();

  await page.getByTestId('upload-input').setInputFiles({
    name: fileName,
    mimeType: 'text/plain',
    buffer: Buffer.from(contents, 'utf8'),
  });

  // The queue leaves 'queued' only once the request is on its way, and leaves
  // 'uploading' only once the API has answered with a stored document.
  const queueItem = page.getByTestId('upload-item').filter({ hasText: fileName });
  await expect(queueItem).toBeVisible();
  await expect(queueItem).toHaveAttribute('data-upload-status', /uploading|indexing|done/);

  // The page inserts this row either from the upload response or from the
  // document.created event, whichever lands first (DocumentManagerPage.tsx),
  // so this asserts that the document reached the library -- not which path
  // delivered it. Proving the realtime path is a separate test's job.
  const row = page.getByTestId('document-row').filter({ hasText: fileName });
  await expect(row).toBeVisible();

  // A real state from the pipeline's own vocabulary. `indexed` is included
  // because a fast worker can finish before this assertion runs; the full
  // journey to `indexed` is asserted in the CI stack test.
  await expect(row.getByTestId('document-status')).toHaveText(/queued|processing|indexed/);
});
