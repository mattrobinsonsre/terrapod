import { defineConfig, devices } from '@playwright/test';
import path from 'path';

const BASE_URL = process.env.BASE_URL || 'http://localhost:3000';
const ADMIN_AUTH = path.join(__dirname, '.auth', 'admin.json');

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // 2 workers per shard in CI: with the suite sharded across many runners,
  // each shard runs few tests, so keeping workers low avoids oversubscribing
  // the 2-vCPU runner while a shard boots its own stack. Tune with shard count.
  workers: process.env.CI ? 2 : 2,
  // CI: each shard emits a `blob` report (auto-named per shard under
  // e2e/blob-report/); the `e2e-report` CI job merges them into one HTML
  // report. `github` gives inline annotations per shard. Local: HTML on failure.
  reporter: process.env.CI
    ? [['blob'], ['github']]
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
    {
      name: 'registry',
      testMatch: 'registry.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'tokens',
      testMatch: 'tokens.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'audit-log',
      testMatch: 'audit-log.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'roles',
      testMatch: 'roles.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'users',
      testMatch: 'users.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'variable-sets',
      testMatch: 'variable-sets.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'runs',
      testMatch: 'runs.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'ai-summary',
      testMatch: 'ai-summary.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      // RBAC negatives set their own per-role storageState (user/audit) inside
      // the spec via test.use(), so no project-level storageState here.
      name: 'rbac-negatives',
      testMatch: 'rbac-negatives.spec.ts',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'manual-lock',
      testMatch: 'manual-lock.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      // Catalog admin + browse run as admin; the in-spec RBAC-negative
      // describe overrides storageState to user.json via test.use().
      name: 'catalog',
      testMatch: 'catalog.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
    {
      name: 'sse-live-update',
      testMatch: 'sse-live-update.spec.ts',
      use: { ...devices['Desktop Chrome'], storageState: ADMIN_AUTH },
    },
  ],
});
