import {
  expect,
  request as apiRequest,
  test as base,
  type APIRequestContext,
  type Page,
} from '@playwright/test';

/** An account created for the run, with the workspace signup gives it. */
export interface TestAccount {
  email: string;
  password: string;
  name: string;
  userId: string;
  workspaceId: string;
}

/** Where the SPA keeps its tokens (see services/apiClient.ts). */
const ACCESS_TOKEN_KEY = 'rs_auth_token';
const REFRESH_TOKEN_KEY = 'rs_refresh_token';

const baseURL = process.env.E2E_BASE_URL ?? 'http://localhost:3000';

/** Character pools for generated passwords, one per class the policy requires. */
const UPPER = 'ABCDEFGHJKLMNPQRSTUVWXYZ';
const LOWER = 'abcdefghijkmnpqrstuvwxyz';
const DIGIT = '23456789';
const SPECIAL = '!@#$%^&*?-_=+';

/** Pick one character uniformly, from the same source the browser would use. */
function pick(pool: string): string {
  return pool[crypto.getRandomValues(new Uint32Array(1))[0] % pool.length];
}

/**
 * A password that satisfies the backend policy, generated per account.
 *
 * The policy (backend/app/core/password_policy.py) rejects a character
 * repeated three times and any four-character run of 'abcdef…', '01234…' or a
 * keyboard row. Drawing characters at random can produce either, so this
 * alternates a letter with a non-letter instead: adjacent characters are never
 * equal, and since every forbidden sequence is all-letters or all-digits, no
 * four consecutive characters can spell one. Alternating the letter case and
 * the digit/special choice covers all four required classes.
 *
 * Nothing is committed and no account is shared beyond the worker that made
 * it, so the suite stays repeatable and no credential reaches the repository.
 */
function generatePassword(): string {
  let password = '';
  for (let pair = 0; pair < 8; pair += 1) {
    password += pick(pair % 2 === 0 ? UPPER : LOWER);
    password += pick(pair % 2 === 0 ? DIGIT : SPECIAL);
  }
  return password;
}

/** Register a fresh account through the real API and return its credentials. */
export async function createAccount(request: APIRequestContext, label: string): Promise<TestAccount> {
  const password = generatePassword();
  // example.com, not example.test: the API validates addresses with
  // email-validator, which refuses reserved names such as .test.
  const email = `e2e-${label}-${crypto.randomUUID().slice(0, 8)}@example.com`;
  const name = `E2E ${label}`;

  const signup = await request.post('/api/v1/auth/signup', { data: { name, email, password } });
  expect(signup.ok(), `signup failed (${signup.status()}): ${await signup.text()}`).toBeTruthy();
  const created = (await signup.json()) as { access_token: string; user: { id: string } };

  const workspaces = await request.get('/api/v1/workspaces', {
    headers: { Authorization: `Bearer ${created.access_token}` },
  });
  expect(workspaces.ok(), `workspace lookup failed (${workspaces.status()})`).toBeTruthy();
  const [workspace] = (await workspaces.json()) as Array<{ id: string }>;
  expect(workspace, 'signup did not create a default workspace').toBeTruthy();

  return { email, password, name, userId: created.user.id, workspaceId: workspace.id };
}

/** Log in through the API and return the tokens the SPA would have stored. */
export async function issueTokens(
  request: APIRequestContext,
  account: TestAccount
): Promise<{ access: string; refresh: string }> {
  const login = await request.post('/api/v1/auth/login', {
    data: { email: account.email, password: account.password },
  });
  expect(login.ok(), `login failed (${login.status()}): ${await login.text()}`).toBeTruthy();
  const tokens = (await login.json()) as { access_token: string; refresh_token: string };
  return { access: tokens.access_token, refresh: tokens.refresh_token };
}

/** Sign in through the real form, the way a user does. */
export async function signIn(page: Page, account: TestAccount): Promise<void> {
  await page.goto('/login');
  await page.getByTestId('login-email').fill(account.email);
  await page.getByTestId('login-password').fill(account.password);
  await page.getByTestId('login-submit').click();
  await expect(page).toHaveURL(/\/dashboard/);
}

interface WorkerFixtures {
  /** One account per worker, with the tokens a signed-in browser would hold. */
  workerAccount: { account: TestAccount; tokens: { access: string; refresh: string } };
}

interface TestFixtures {
  /** A fresh account for this test alone, for anything needing its own tenant. */
  account: TestAccount;
  /** A page that starts already signed in as the worker's account. */
  signedInPage: Page;
  /** The worker account's details, for tests that assert against its workspace. */
  currentAccount: TestAccount;
}

/**
 * The API rate-limits every /auth/ path to 20 requests a minute per client IP,
 * and the SPA calls /auth/me on each mount. Signing up and logging in once per
 * worker -- rather than once per test -- keeps a run well inside that budget,
 * which is why a signed-in page is seeded from tokens instead of repeating the
 * sign-in form. The form itself is covered by auth.spec.ts.
 */
export const test = base.extend<TestFixtures, WorkerFixtures>({
  workerAccount: [
    // Playwright requires an object pattern here even with no dependencies.
    async ({}, use, workerInfo) => {
      const context = await apiRequest.newContext({ baseURL });
      const account = await createAccount(context, `w${workerInfo.workerIndex}`);
      const tokens = await issueTokens(context, account);
      await use({ account, tokens });
      await context.dispose();
    },
    { scope: 'worker' },
  ],

  account: async ({ request }, use, testInfo) => {
    const label = testInfo.title.replace(/[^a-z0-9]+/gi, '-').slice(0, 20).toLowerCase();
    await use(await createAccount(request, label));
  },

  currentAccount: async ({ workerAccount }, use) => {
    await use(workerAccount.account);
  },

  signedInPage: async ({ page, workerAccount }, use) => {
    const { access, refresh } = workerAccount.tokens;
    // Seeded before any script runs, through the same localStorage contract the
    // app itself uses; the API, the session and every request stay real.
    await page.addInitScript(
      ([accessKey, refreshKey, accessToken, refreshToken]) => {
        window.localStorage.setItem(accessKey, accessToken);
        window.localStorage.setItem(refreshKey, refreshToken);
      },
      [ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, access, refresh] as const
    );
    await use(page);
  },
});

export { expect };
