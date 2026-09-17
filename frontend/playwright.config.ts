import { defineConfig, devices } from '@playwright/test';

/**
 * Browser end-to-end tests: the real frontend against the real backend.
 *
 * The suite drives a browser through the application the way a user does, so
 * nothing here is mocked -- not the API, not the WebSocket, not ingestion.
 * The services it needs (PostgreSQL, Redis, Qdrant, the API and a Celery
 * worker) are started by CI or by the developer; see e2e/README.md.
 *
 * The frontend is served by the Vite dev server because it proxies both /api
 * and the WebSocket upgrade to the backend, which makes the browser's requests
 * same-origin exactly as they are behind the production reverse proxy. The
 * production bundle itself is verified by the Docker job in ci.yml.
 */
const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:3000';

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  // The API rate-limits /auth/ and /upload to 20 requests per minute per
  // client IP, and every worker would share one IP. A single worker keeps the
  // suite inside that budget and keeps realtime assertions easy to read.
  workers: 1,
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  // No retries: a test that only passes on a second attempt is a flaky test,
  // and hiding that would defeat the point of this suite.
  retries: 0,
  // Indexing runs a real embedding model and a real vector upsert.
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: [
    ['list'],
    ['html', { open: 'never', outputFolder: 'playwright-report' }],
    ['json', { outputFile: 'playwright-report/results.json' }],
  ],
  use: {
    baseURL,
    actionTimeout: 15_000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npm run dev -- --port 3000 --strictPort',
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
