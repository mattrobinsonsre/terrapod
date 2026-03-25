import { test, expect } from '@playwright/test';

const ADMIN_EMAIL = 'admin@terrapod.local';
const ADMIN_PASSWORD = 'TestPassword123!';

test.describe('Authentication', () => {
  test('valid login redirects to home and sets auth state', async ({ page }) => {
    await page.goto('/login');
    await page.waitForSelector('#email', { timeout: 15_000 });

    await page.fill('#email', ADMIN_EMAIL);
    await page.fill('#password', ADMIN_PASSWORD);
    await page.click('button[type="submit"]');

    // Should redirect away from /login
    await page.waitForURL((url) => !url.pathname.startsWith('/login'), {
      timeout: 15_000,
    });

    // Verify localStorage auth state was set
    const authState = await page.evaluate(() =>
      localStorage.getItem('terrapod_auth'),
    );
    expect(authState).toBeTruthy();

    const parsed = JSON.parse(authState!);
    expect(parsed.email).toBe(ADMIN_EMAIL);
    expect(parsed.token).toBeTruthy();
    expect(parsed.roles).toContain('admin');
  });

  test('invalid credentials show error and stay on login', async ({ page }) => {
    await page.goto('/login');
    await page.waitForSelector('#email', { timeout: 15_000 });

    await page.fill('#email', 'wrong@example.com');
    await page.fill('#password', 'badpassword');
    await page.click('button[type="submit"]');

    // Error message should appear
    const error = page.locator('.bg-red-900\\/30');
    await expect(error).toBeVisible({ timeout: 10_000 });

    // Should still be on /login
    expect(page.url()).toContain('/login');
  });

  test('logout clears auth and redirects to login', async ({ page }) => {
    // First, log in
    await page.goto('/login');
    await page.waitForSelector('#email', { timeout: 15_000 });
    await page.fill('#email', ADMIN_EMAIL);
    await page.fill('#password', ADMIN_PASSWORD);
    await page.click('button[type="submit"]');
    await page.waitForURL((url) => !url.pathname.startsWith('/login'), {
      timeout: 15_000,
    });

    // Click the logout button in the nav bar
    await page.click('button:has-text("Logout")');

    // Should redirect to /login
    await page.waitForURL('**/login', { timeout: 10_000 });

    // localStorage should be cleared
    const authState = await page.evaluate(() =>
      localStorage.getItem('terrapod_auth'),
    );
    expect(authState).toBeNull();
  });
});
