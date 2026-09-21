import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import { request as apiRequest, type APIRequestContext, type APIResponse } from '@playwright/test';

import { ACTIVE_WORKSPACE_KEY, expect, signedInStorage, signUp, test } from './fixtures';

const SAMPLE = new URL('./fixtures/sample-document.txt', import.meta.url);
const EVIDENCE = new URL('../test-results/workspace-isolation.json', import.meta.url);
const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:3000';

/**
 * Generous for one small file: with no Celery broker (a partial local stack)
 * the API indexes inline, inside the upload request.
 */
const UPLOAD_TIMEOUT_MS = 120_000;

interface Probe {
  name: string;
  method: string;
  path: string;
  status: number;
  expected: string;
  held: boolean;
  /** The response carried something only a member of Workspace A could know. */
  leaked: boolean;
}

test.describe('workspace isolation', () => {
  test.setTimeout(UPLOAD_TIMEOUT_MS + 120_000);

  /**
   * One account cannot reach another account's workspace or documents.
   *
   * A is the worker's account, B a second account signed up for this test,
   * each owning the workspace signup gave it. A puts one document into
   * Workspace A; B then tries every route that could expose it -- listing,
   * searching, status, download, delete, the workspace itself, analytics and
   * chat -- and is refused each time with 404, which is also what a route
   * says about an id that does not exist, so B cannot even learn that A's
   * workspace or document is there. In the browser, B starts with a
   * remembered workspace id pointing at A's, the state a shared machine can
   * leave behind, and must still land in its own workspace without asking the
   * API for A's. Afterwards A can still see, read and download its document.
   *
   * Isolation does not depend on the worker, Redis or Qdrant, so this runs on
   * a partial local stack as well as in CI, and has nothing to skip.
   *
   * Sensitive requests (the shared 20-a-minute /auth/ + /upload budget): one
   * signup for B, one upload for A, and two /auth/me for each SPA mount, one
   * for B and one for A -- six. B's probes and every other call here are on
   * the ordinary limit.
   */
  test("another workspace cannot list, read, download or delete a document", async ({
    browser,
    request,
    signedInPage: pageA,
    workerAccount,
  }, testInfo) => {
    const { account: accountA, tokens: tokensA } = workerAccount;
    const workspaceA = accountA.workspaceId;

    // --- two accounts, two workspaces --------------------------------------
    const { account: accountB, tokens: tokensB } = await signUp(request, 'iso-b');
    const workspaceB = accountB.workspaceId;
    expect(workspaceB, 'both accounts ended up in the same workspace').not.toBe(workspaceA);

    const asA = await apiRequest.newContext({
      baseURL,
      extraHTTPHeaders: { Authorization: `Bearer ${tokensA.access}` },
    });
    const asB = await apiRequest.newContext({
      baseURL,
      extraHTTPHeaders: { Authorization: `Bearer ${tokensB.access}` },
    });
    const contextB = await browser.newContext({
      baseURL,
      // B's own session, plus A's workspace id remembered from before.
      storageState: signedInStorage(tokensB, { [ACTIVE_WORKSPACE_KEY]: workspaceA }),
    });

    try {
      const workspaceName = async (api: APIRequestContext, id: string) => {
        const listed = await api.get('/api/v1/workspaces');
        expect(listed.ok(), `workspace list failed (${listed.status()})`).toBeTruthy();
        const rows = (await listed.json()) as Array<{ id: string; name: string }>;
        const row = rows.find((w) => w.id === id);
        expect(row, `workspace ${id} is not in its own owner's list`).toBeTruthy();
        return row!.name;
      };
      const nameA = await workspaceName(asA, workspaceA);
      const nameB = await workspaceName(asB, workspaceB);
      expect(nameB, 'the two workspaces cannot be told apart by name').not.toBe(nameA);

      // --- one document in Workspace A ---------------------------------------
      const marker = crypto.randomUUID();
      const fileName = `e2e-isolation-${marker.slice(0, 8)}.txt`;
      const contents = `${readFileSync(SAMPLE, 'utf8')}\nIsolation run marker: ${marker}\n`;
      const uploaded = await asA.post('/api/v1/documents/upload', {
        multipart: {
          file: { name: fileName, mimeType: 'text/plain', buffer: Buffer.from(contents, 'utf8') },
          workspace_id: workspaceA,
        },
        timeout: UPLOAD_TIMEOUT_MS,
      });
      expect(uploaded.ok(), `A's upload failed (${uploaded.status()}): ${await uploaded.text()}`)
        .toBeTruthy();
      const document = (await uploaded.json()) as { id: string; title: string; duplicate?: boolean };
      expect(document.duplicate, 'the upload was treated as a duplicate').toBeFalsy();
      const documentId = document.id;

      // The control: the document is really there, for its owner. Without
      // this, B failing to find it would prove nothing.
      const ownList = await asA.get('/api/v1/documents', {
        params: { workspace_id: workspaceA, search: fileName },
      });
      expect(ownList.ok()).toBeTruthy();
      expect(((await ownList.json()) as { items: Array<{ id: string }> }).items.map((d) => d.id))
        .toContain(documentId);

      // --- B, against the API ------------------------------------------------
      const secretsOfA = [fileName, nameA, marker];
      const probes: Probe[] = [];
      const record = async (
        name: string,
        method: string,
        path: string,
        response: APIResponse,
        expected: string,
        held: boolean,
        alsoSecret: string[] = []
      ) => {
        const body = await response.text();
        const leaked = [...secretsOfA, ...alsoSecret].some((secret) => body.includes(secret));
        probes.push({ name, method, path, status: response.status(), expected, held, leaked });
      };
      const refused = async (name: string, method: string, path: string, response: APIResponse) =>
        record(name, method, path, response, '404', response.status() === 404);
      const absent = async (name: string, path: string, response: APIResponse) => {
        const ok = response.ok();
        const ids = ok
          ? ((await response.json()) as { items: Array<{ id: string }> }).items.map((d) => d.id)
          : [];
        // The id counts as a secret here: B never sent it on these requests.
        await record(name, 'GET', path, response, '200 without the document', ok && !ids.includes(documentId), [
          documentId,
        ]);
      };

      await refused('list Workspace A', 'GET', '/api/v1/documents?workspace_id=A',
        await asB.get('/api/v1/documents', { params: { workspace_id: workspaceA } }));
      await absent('list own workspace', '/api/v1/documents',
        await asB.get('/api/v1/documents'));
      await absent('search own workspace for the file', '/api/v1/documents?search=<file>',
        await asB.get('/api/v1/documents', { params: { search: fileName } }));
      await refused('document status', 'GET', '/api/v1/documents/{id}/status',
        await asB.get(`/api/v1/documents/${documentId}/status`));
      await refused('document download', 'GET', '/api/v1/documents/{id}/download',
        await asB.get(`/api/v1/documents/${documentId}/download`, { maxRedirects: 0 }));
      await refused('document delete', 'DELETE', '/api/v1/documents/{id}',
        await asB.delete(`/api/v1/documents/${documentId}`));
      await refused('workspace detail', 'GET', '/api/v1/workspaces/A',
        await asB.get(`/api/v1/workspaces/${workspaceA}`));
      await refused('analytics for Workspace A', 'GET', '/api/v1/analytics?workspace_id=A',
        await asB.get('/api/v1/analytics', { params: { workspace_id: workspaceA } }));
      // Refused at workspace resolution, before any retrieval or model call.
      await refused('chat against Workspace A', 'POST', '/api/v1/chat/stream',
        await asB.post('/api/v1/chat/stream', {
          data: { prompt: 'isolation probe', workspace_id: workspaceA },
        }));

      const listedForB = await asB.get('/api/v1/workspaces');
      const workspaceIdsForB = listedForB.ok()
        ? ((await listedForB.json()) as Array<{ id: string }>).map((w) => w.id)
        : [];
      await record('list workspaces', 'GET', '/api/v1/workspaces', listedForB, 'only Workspace B',
        listedForB.ok() && workspaceIdsForB.length === 1 && workspaceIdsForB[0] === workspaceB,
        [workspaceA]);

      for (const probe of probes) {
        expect(probe.held, `${probe.name}: ${probe.method} ${probe.path} answered ${probe.status}`)
          .toBe(true);
        expect(probe.leaked, `${probe.name}: the response carried Workspace A's data`).toBe(false);
      }

      // B's delete attempt changed nothing.
      const afterDelete = await asA.get(`/api/v1/documents/${documentId}/status`);
      expect(afterDelete.status(), "A's document did not survive B's delete attempt").toBe(200);

      // --- B, in the browser -------------------------------------------------
      const pageB = await contextB.newPage();
      const requestsB: string[] = [];
      pageB.on('request', (r) => requestsB.push(r.url()));
      const socketsB: string[] = [];
      pageB.on('websocket', (ws) => socketsB.push(ws.url()));

      const documentsListedForB = pageB.waitForResponse(
        (r) => new URL(r.url()).pathname === '/api/v1/documents' && r.request().method() === 'GET'
      );
      await pageB.goto('/documents');
      await expect(pageB.getByTestId('app-shell')).toBeVisible();
      const listResponseB = await documentsListedForB;
      expect(listResponseB.status()).toBe(200);
      const listedWorkspaceB = new URL(listResponseB.url()).searchParams.get('workspace_id');
      expect(listedWorkspaceB, 'B\'s page asked for documents from the wrong workspace').toBe(workspaceB);

      // The workspace switcher names B's workspace, not the remembered one.
      const switcher = pageB.locator('button[aria-haspopup="listbox"]');
      await expect(switcher).toHaveText(nameB);
      // B's workspace is empty, and says so once its list has loaded.
      await expect(pageB.getByRole('heading', { name: 'No documents yet' })).toBeVisible();
      await expect(pageB.getByTestId('document-row')).toHaveCount(0);
      await expect(pageB.getByText(fileName)).toHaveCount(0);

      const documentRequestsB = requestsB.filter(
        (url) => new URL(url).pathname.startsWith('/api/v1/documents')
      );
      const requestsNamingA = [...requestsB, ...socketsB].filter((url) => url.includes(workspaceA));
      expect(documentRequestsB.length, 'B\'s page never loaded its documents').toBeGreaterThan(0);
      expect(requestsNamingA, 'B\'s page asked the API about Workspace A').toEqual([]);

      // --- A still has its document ------------------------------------------
      await pageA.goto('/documents');
      await expect(pageA.getByTestId('app-shell')).toBeVisible();
      const rowForA = pageA.getByTestId('document-row').filter({ hasText: fileName });
      await expect(rowForA).toBeVisible();
      const statusForA = await asA.get(`/api/v1/documents/${documentId}/status`);
      expect(statusForA.status()).toBe(200);
      const downloadForA = await asA.get(`/api/v1/documents/${documentId}/download`);
      expect(downloadForA.status()).toBe(200);
      const downloaded = await downloadForA.text();
      expect(downloaded, 'A downloaded something other than its file').toContain(marker);

      // Ids only: no tokens, passwords, emails or names of people.
      const evidence = {
        workspaceA,
        workspaceB,
        documentId,
        probes,
        documentIntactAfterDelete: afterDelete.status() === 200,
        // Read back from the page and the API as it is written, not assumed.
        ui: {
          rememberedWorkspace: 'A',
          documentsRequestedFor: listedWorkspaceB === workspaceB ? 'B' : 'other',
          switcherShowsB: (await switcher.innerText()).trim() === nameB,
          switcherShowsA: (await switcher.innerText()).includes(nameA),
          documentRequests: documentRequestsB.length,
          requestsNamingWorkspaceA: requestsNamingA.length,
          documentRendered: (await pageB.getByText(fileName).count()) > 0,
        },
        ownerAccess: {
          rowVisible: await rowForA.isVisible(),
          status: statusForA.status(),
          download: downloadForA.status(),
          downloadMatches: downloaded.includes(marker),
        },
      };
      mkdirSync(dirname(fileURLToPath(EVIDENCE)), { recursive: true });
      writeFileSync(fileURLToPath(EVIDENCE), JSON.stringify(evidence, null, 2), 'utf8');
      await testInfo.attach('workspace-isolation', {
        body: JSON.stringify(evidence, null, 2),
        contentType: 'application/json',
      });
      console.log(`[e2e] isolation: ${JSON.stringify({ ...evidence, probes: probes.map((p) => `${p.name}=${p.status}`) })}`);
    } finally {
      await contextB.close();
      await asA.dispose();
      await asB.dispose();
    }
  });
});
