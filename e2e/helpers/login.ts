/**
 * UI login helper — performs login through the actual login page.
 */
import type { Page } from '@playwright/test';

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';

export async function performLogin(
  page: Page,
  email: string,
  password: string,
): Promise<void> {
  await page.goto(`${BASE_URL}/login`);

  // Wait for the login form to load (providers fetched)
  await page.waitForSelector('#email', { timeout: 15_000 });

  await page.fill('#email', email);
  await page.fill('#password', password);
  await page.click('button[type="submit"]');

  // Wait for redirect away from /login
  await page.waitForURL((url) => !url.pathname.startsWith('/login'), {
    timeout: 15_000,
  });
}
