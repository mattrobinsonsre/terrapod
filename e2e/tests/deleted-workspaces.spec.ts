import { test, expect } from '@playwright/test';
import {
  getStoredToken,
  createWorkspace,
  seedStateVersionWithContent,
  uniqueName,
} from '../helpers/api';
import path from 'path';

const API_URL = process.env.API_URL || 'http://localhost:8000';
const USER_AUTH = path.join(__dirname, '..', '.auth', 'user.json');

/**
 * Undelete surface (#1253).
 *
 * These deliberately SEED a deleted workspace rather than loading the page as
 * it happens to be. A page assertion that runs against an empty list proves
 * nothing — the table isn't hidden by the breakpoint, it simply isn't rendered
 * — so it passes whatever the component does. Every test here creates a real
 * workspace, gives it real state, deletes it, and only then looks at the page.
 */

/** Create a workspace with state and delete it, so a marker exists. */
async function createAndDelete(
  token: string,
  name: string,
): Promise<string> {
  // auto-apply ON so the 'comes back inert' assertion has something to prove.
  const wsId = await createWorkspace(token, name, { 'auto-apply': true });
  await seedStateVersionWithContent(token, wsId, [
    { mode: 'managed', type: 'null_resource', name: 'a', instances: [{ attributes: {} }] },
  ]);
  const res = await fetch(`${API_URL}/api/terrapod/v1/workspaces/${wsId}`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok && res.status !== 204) {
    throw new Error(`Delete workspace failed: ${res.status} ${await res.text()}`);
  }
  return wsId.replace(/^ws-/, '');
}

test.describe('Deleted workspaces (undelete)', () => {
  test('a deleted workspace appears with its recoverable state', async ({ page }) => {
    const token = getStoredToken();
    const name = uniqueName('e2edel');
    const rawId = await createAndDelete(token, name);

    await page.goto('/admin/deleted-workspaces');
    const row = page.locator(`tr:has-text("${name}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });

    // The id is shown so an operator can correlate with the bucket.
    await expect(row).toContainText(rawId);
    // One state version was seeded, and the count is read from storage rather
    // than from the marker — so this also proves the blobs actually survived
    // the delete, which is the whole premise of the feature. Asserted on the
    // specific cell: a bare toContainText('1') would match any timestamp that
    // happens to contain a 1.
    await expect(row.locator('td').nth(2)).toHaveText('1');
  });

  test('restoring produces a NEW workspace, inert, and keeps the original listed', async ({
    page,
  }) => {
    const token = getStoredToken();
    const name = uniqueName('e2erestore');
    const rawId = await createAndDelete(token, name);

    await page.goto('/admin/deleted-workspaces');
    const row = page.locator(`tr:has-text("${name}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });

    // Restore is guarded by a native confirm() on every pointer type — a
    // salvage is never a casual click. Accept it and assert the dialog says
    // what the operator actually gets.
    let dialogText = '';
    page.once('dialog', (d) => {
      dialogText = d.message();
      void d.accept();
    });

    // Read the RESPONSE rather than inferring success from on-screen text.
    // The success copy starts "Recovered ..." and the 409 reads "No state
    // could be recovered", so any case-insensitive match on "recovered"
    // passes on failure too — the test would sail past a broken restore and
    // then fail later somewhere less informative.
    const [res] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/restore') && r.request().method() === 'POST',
        { timeout: 20_000 },
      ),
      row.getByRole('button', { name: /restore/i }).click(),
    ]);
    expect(res.status(), `restore failed: ${await res.text()}`).toBe(201);
    expect(dialogText).toContain('NEW');

    const restored = (await res.json()).data;
    expect(restored.id.replace(/^ws-/, ''), 'restore must mint a new id').not.toBe(rawId);
    expect(restored.attributes['state-versions-restored']).toBe(1);
    // Auto-apply was ON before the delete and must come back OFF.
    expect(restored.attributes.suppressed).toContain('auto_apply');

    // The source is a copy, not a move: the original stays recoverable until
    // its retention window closes.
    await expect(page.locator(`tr:has-text("${name}")`).first()).toBeVisible();
  });

  test('an anonymous caller cannot reach the undelete surface', async ({ browser }) => {
    const ctx = await browser.newContext({ storageState: undefined });
    const page = await ctx.newPage();
    const res = await page.request.get(
      `${API_URL}/api/terrapod/v1/deleted-workspaces`,
    );
    expect([401, 403]).toContain(res.status());
    await ctx.close();
  });

  test('an authenticated non-admin cannot reach the undelete surface', async ({ browser }) => {
    // This is the test that matters, and it was missing (#1297). The
    // anonymous case above passes on 401 alone, so it would still be green if
    // `require_admin` were downgraded to `get_current_user` — the exact
    // regression that would expose every marker to every logged-in user.
    //
    // Reads are admin-only for the same reason writes are: a marker names a
    // workspace, its labels and its variable names, and a restore
    // materialises its state — and therefore its secrets — into a workspace
    // the caller can then read. The original's ACL died with its rows, so
    // there is nothing to delegate to.
    const ctx = await browser.newContext({ storageState: USER_AUTH });
    const page = await ctx.newPage();

    for (const path of [
      '/api/terrapod/v1/deleted-workspaces',
      '/api/terrapod/v1/deleted-workspaces/00000000-0000-4000-8000-000000000000',
    ]) {
      const res = await page.request.get(`${API_URL}${path}`);
      expect(res.status(), `${path} must be forbidden for a non-admin`).toBe(403);
    }

    // ...and the restore itself, which is the one that would materialise
    // somebody else's secrets.
    const restore = await page.request.post(
      `${API_URL}/api/terrapod/v1/deleted-workspaces/00000000-0000-4000-8000-000000000000/restore`,
    );
    expect(restore.status()).toBe(403);

    await ctx.close();
  });
});
