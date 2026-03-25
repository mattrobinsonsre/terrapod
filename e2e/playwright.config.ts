import { defineConfig, devices } from '@playwright/test';
import path from 'path';

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';
const ADMIN_AUTH = path.join(__dirname, '.auth', 'admin.json');

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 3 : 2,
  reporter: process.env.CI
    ? [['html', { open: 'never' }], ['github']]
    : [['html', { open: 'on-failure' }]],

  globalSetup: './global-setup.ts',

  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 10_000,
  },

  timeout: 30_000,
  expect: { timeout: 10_000 },

  projects: [
    {
      name: 'auth',
      testMatch: 'auth.spec.ts',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'workspaces',
      testMatch: 'workspaces.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'variables',
      testMatch: 'variables.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'admin',
      testMatch: 'admin.spec.ts',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'navigation',
      testMatch: 'navigation.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
  ],
});
