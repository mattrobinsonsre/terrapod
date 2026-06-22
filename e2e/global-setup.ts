/**
 * Global setup: wait for stack, log in as admin + regular user, save storageState.
 */
import { chromium, type FullConfig } from '@playwright/test';
import path from 'path';
import fs from 'fs';
import {
  waitForStack,
  getSessionToken,
  createUser,
  setRoleAssignments,
} from './helpers/api.js';
import { performLogin } from './helpers/login.js';

const ADMIN_EMAIL = 'admin@terrapod.local';
const ADMIN_PASSWORD = 'TestPassword123!';
const USER_EMAIL = 'e2e-user@terrapod.local';
const USER_PASSWORD = 'TestPassword123!';
// Read-only `audit` user — for RBAC tests that assert "can view, cannot write".
const AUDIT_EMAIL = 'e2e-audit@terrapod.local';
const AUDIT_PASSWORD = 'TestPassword123!';

const AUTH_DIR = path.join(__dirname, '.auth');
const ADMIN_AUTH = path.join(AUTH_DIR, 'admin.json');
const USER_AUTH = path.join(AUTH_DIR, 'user.json');
const AUDIT_AUTH = path.join(AUTH_DIR, 'audit.json');

export default async function globalSetup(_config: FullConfig) {
  fs.mkdirSync(AUTH_DIR, { recursive: true });

  // Wait for the full stack to be healthy
  console.log('Waiting for stack to be healthy...');
  await waitForStack();
  console.log('Stack is healthy');

  // Get an admin API token for user creation
  const { token: adminToken } = await getSessionToken(ADMIN_EMAIL, ADMIN_PASSWORD);

  // Create regular user (idempotent — 409 is ok)
  await createUser(adminToken, USER_EMAIL, USER_PASSWORD, 'E2E User');

  // Create audit user + grant the read-only `audit` platform role.
  await createUser(adminToken, AUDIT_EMAIL, AUDIT_PASSWORD, 'E2E Audit');
  await setRoleAssignments(adminToken, AUDIT_EMAIL, ['audit']);

  // Launch browser and log in as admin via UI
  const browser = await chromium.launch();

  const adminContext = await browser.newContext();
  const adminPage = await adminContext.newPage();
  await performLogin(adminPage, ADMIN_EMAIL, ADMIN_PASSWORD);
  await adminContext.storageState({ path: ADMIN_AUTH });
  await adminContext.close();

  // Log in as regular user via UI
  const userContext = await browser.newContext();
  const userPage = await userContext.newPage();
  await performLogin(userPage, USER_EMAIL, USER_PASSWORD);
  await userContext.storageState({ path: USER_AUTH });
  await userContext.close();

  // Log in as audit user via UI
  const auditContext = await browser.newContext();
  const auditPage = await auditContext.newPage();
  await performLogin(auditPage, AUDIT_EMAIL, AUDIT_PASSWORD);
  await auditContext.storageState({ path: AUDIT_AUTH });
  await auditContext.close();

  await browser.close();

  console.log('Global setup complete — auth states saved');
}
