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

  test('session-status endpoint reflects the true TTL; banner stays hidden on a fresh session (#726)', async ({ page }) => {
    await page.goto('/login');
    await page.waitForSelector('#email', { timeout: 15_000 });
    await page.fill('#email', ADMIN_EMAIL);
    await page.fill('#password', ADMIN_PASSWORD);
    await page.click('button[type="submit"]');
    await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 15_000 });

    // A fresh 12h session is nowhere near expiry, so the amber expiry banner
    // must NOT show. The old banner warned falsely off a stale local clock; the
    // fix reconciles against the server's true TTL (#726).
    await expect(page.getByText(/Session expires in/)).toHaveCount(0);

    // The banner reconciles against GET /auth/session. Prove it works through
    // the full BFF proxy chain and returns a large positive TTL (well beyond
    // the 5-minute warning threshold) rather than a stale/near-zero value.
    const token = await page.evaluate(
      () => JSON.parse(localStorage.getItem('terrapod_auth')!).token,
    );
    const res = await page.request.get('/api/terrapod/v1/auth/session', {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.authenticated).toBe(true);
    expect(typeof body.ttl_seconds).toBe('number');
    expect(body.ttl_seconds).toBeGreaterThan(10 * 60);

    // A bogus token is rejected with 401 — the authoritative "log back in"
    // signal the banner (via apiFetch) acts on, instead of a local countdown.
    const bad = await page.request.get('/api/terrapod/v1/auth/session', {
      headers: { Authorization: 'Bearer not-a-real-session' },
    });
    expect(bad.status()).toBe(401);
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

    // Log out now lives inside the Account menu (whose desktop trigger is
    // labelled with the signed-in user's email) — #719 grouped-nav IA.
    await page.getByRole('button', { name: ADMIN_EMAIL }).click();
    await page.getByRole('menuitem', { name: 'Log out' }).click();

    // Should redirect to /login
    await page.waitForURL('**/login', { timeout: 10_000 });

    // localStorage should be cleared
    const authState = await page.evaluate(() =>
      localStorage.getItem('terrapod_auth'),
    );
    expect(authState).toBeNull();
  });
});
