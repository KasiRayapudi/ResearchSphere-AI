import { expect, signIn, test } from './fixtures';

/**
 * Authentication through the real form against the real API.
 *
 * The account is registered over the API by the `account` fixture, so the
 * browser work under test is the sign-in itself rather than filling in the
 * signup form; every test gets its own account and its own workspace.
 */
test.describe('authentication', () => {
  test('an unauthenticated visitor cannot reach the dashboard', async ({ page }) => {
    await page.goto('/dashboard');

    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByTestId('login-submit')).toBeVisible();
    await expect(page.getByTestId('app-shell')).toHaveCount(0);
  });

  test('signing in reaches the authenticated application', async ({ page, account }) => {
    await signIn(page, account);
    await expect(page.getByTestId('app-shell')).toBeVisible();

    // The session survives a reload: the token is persisted, not held in memory.
    await page.reload();
    await expect(page.getByTestId('app-shell')).toBeVisible();

    // And an authenticated visitor is kept away from the sign-in page.
    await page.goto('/login');
    await expect(page).toHaveURL(/\/dashboard/);
  });

  test('a wrong password is refused', async ({ page, account }) => {
    await page.goto('/login');
    await page.getByTestId('login-email').fill(account.email);
    await page.getByTestId('login-password').fill(`${account.password}-wrong`);
    await page.getByTestId('login-submit').click();

    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByTestId('app-shell')).toHaveCount(0);
  });
});
