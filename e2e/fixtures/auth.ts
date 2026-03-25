/**
 * Playwright fixtures for admin and regular user pages.
 */
import { test as base, type Page, type BrowserContext } from '@playwright/test';
import path from 'path';

const ADMIN_AUTH = path.join(__dirname, '..', '.auth', 'admin.json');
const USER_AUTH = path.join(__dirname, '..', '.auth', 'user.json');

type AuthFixtures = {
  adminPage: Page;
  userPage: Page;
};

export const test = base.extend<AuthFixtures>({
  adminPage: async ({ browser }, use) => {
    const context: BrowserContext = await browser.newContext({
      storageState: ADMIN_AUTH,
    });
    const page = await context.newPage();
    await use(page);
    await context.close();
  },

  userPage: async ({ browser }, use) => {
    const context: BrowserContext = await browser.newContext({
      storageState: USER_AUTH,
    });
    const page = await context.newPage();
    await use(page);
    await context.close();
  },
});

export { expect } from '@playwright/test';
