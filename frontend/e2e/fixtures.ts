import { expect, test as base, type APIRequestContext, type Page } from '@playwright/test';

/** An account created for one test, with the workspace signup gives it. */
export interface TestAccount {
  email: string;
  password: string;
  name: string;
  userId: string;
  workspaceId: string;
}

/**
 * A password that satisfies the backend policy, generated per account.
 *
 * Nothing is committed and no account is shared: credentials exist only for
 * the life of the run, which is what keeps this suite repeatable and keeps
 * secrets out of the repository.
 */
function generatePassword(): string {
  return `E2e-${crypto.randomUUID().replace(/-/g, '').slice(0, 20)}-!7`;
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

/** Sign in through the real form, the way a user does. */
export async function signIn(page: Page, account: TestAccount): Promise<void> {
  await page.goto('/login');
  await page.getByTestId('login-email').fill(account.email);
  await page.getByTestId('login-password').fill(account.password);
  await page.getByTestId('login-submit').click();
  await expect(page).toHaveURL(/\/dashboard/);
}

/** `test` with an `account` fixture: one isolated account and workspace per test. */
export const test = base.extend<{ account: TestAccount }>({
  account: async ({ request }, use, testInfo) => {
    const label = testInfo.title.replace(/[^a-z0-9]+/gi, '-').slice(0, 20).toLowerCase();
    await use(await createAccount(request, label));
  },
});

export { expect };
