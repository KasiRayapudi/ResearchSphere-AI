import type { FullConfig } from '@playwright/test';

/** The status the API reports for a dependency it can reach. */
const REACHABLE = 'ok';

/** Services the full journey needs: upload -> Celery -> embedding -> Qdrant -> realtime. */
const REQUIRED_SERVICES = ['database', 'redis', 'qdrant'];

type ReadyPayload = { status?: string; checks?: Record<string, string>; failed?: string[] };

/**
 * Wait until the API answers, then decide whether the stack is good enough.
 *
 * `webServer` in the config waits for the frontend; this waits for what is
 * behind it. /api/ready is the application's own readiness condition, so no
 * test has to guess how long start-up takes.
 *
 * In CI the whole stack must be up, and this says so before the first test
 * runs rather than letting a missing service surface as a puzzling timeout
 * inside the indexing test. Locally a developer may deliberately run part of
 * the stack, so the missing pieces are reported and the run continues; the
 * tests that need those services will fail on their own terms.
 */
async function waitForApi(baseURL: string, timeoutMs: number): Promise<{ status: number; payload: ReadyPayload }> {
  const deadline = Date.now() + timeoutMs;
  let lastReason = 'no attempt made';

  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseURL}/api/ready`);
      const payload = (await response.json().catch(() => ({}))) as ReadyPayload;
      return { status: response.status, payload };
    } catch (error) {
      lastReason = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }

  throw new Error(
    `The API behind ${baseURL}/api/ready did not answer within ${timeoutMs}ms. Last result: ${lastReason}. ` +
      'Start the backend (and its services) before running the end-to-end suite; see e2e/README.md.'
  );
}

export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0]?.use?.baseURL ?? 'http://localhost:3000';
  const { status, payload } = await waitForApi(
    baseURL,
    Number(process.env.E2E_API_READY_TIMEOUT_MS ?? 180_000)
  );
  const checks = payload.checks ?? {};
  const missing = REQUIRED_SERVICES.filter((name) => checks[name] !== REACHABLE);

  const summary = missing.length > 0 ? `missing: ${missing.join(', ')}` : 'all required services reachable';
  const report = `[e2e] API answered /api/ready with ${status}; ${summary}. Checks: ${JSON.stringify(checks)}`;

  if (!process.env.CI) {
    console.log(report);
    return;
  }

  // The HTTP status is reported but is deliberately not the gate. /api/ready
  // also covers GEMINI_API_KEY, which none of these journeys exercise and
  // which CI holds no key for, so a green stack still answers 503. Gating on
  // the code would mean planting a fake key to satisfy an unrelated probe;
  // the services this suite really depends on are asserted directly instead.
  if (missing.length > 0) {
    throw new Error(
      `The end-to-end stack is incomplete: these services are not reachable: ${missing.join(', ')}. ` +
        `/api/ready returned ${status} and reported: ${JSON.stringify(checks)}`
    );
  }

  console.log(report);
}
