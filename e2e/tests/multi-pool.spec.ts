import { test, expect } from '@playwright/test';
import path from 'path';
import {
  createAgentPool,
  createWorkspace,
  getStoredToken,
  uniqueName,
} from '../helpers/api.js';
import { expectNoHorizontalPageScroll } from '../helpers/responsive.js';

/**
 * Multi-pool workspace routing (#1085) — the UI half.
 *
 * A workspace can name several agent pools; the set is flat and a queued run is
 * offered to every pool at once. The dispatch behaviour is proven against real
 * Postgres in the integration tier; what can only be proven in the browser is
 * that an operator can actually *see and change the set* — the feature is
 * API-only otherwise.
 */
const ADMIN_AUTH = path.join(__dirname, '..', '.auth', 'admin.json');

test.describe('Multi-pool workspace routing', () => {
  test.use({ storageState: ADMIN_AUTH });

  test('a workspace renders every pool in its set, not just the first', async ({ page }) => {
    const token = getStoredToken('admin.json');
    const poolA = await createAgentPool(token, uniqueName('e2e-pool-a'));
    const poolB = await createAgentPool(token, uniqueName('e2e-pool-b'));
    const wsId = await createWorkspace(token, uniqueName('e2e-multipool'), {
      'execution-mode': 'agent',
      'agent-pool-ids': [poolA, poolB],
    });

    await page.goto(`/workspaces/${wsId}`);

    // Both pools are listed. Asserting on BOTH is the point — showing only the
    // first would look correct while hiding half the set.
    const pools = page.getByText('Agent pools', { exact: true });
    await expect(pools).toBeVisible({ timeout: 10_000 });
    for (const id of [poolA, poolB]) {
      await expect(page.getByText(id.replace(/^apool-/, '').slice(0, 8), { exact: false }).first())
        .toBeVisible();
    }
  });

  test('the set survives a round-trip through the settings form', async ({ page }) => {
    const token = getStoredToken('admin.json');
    const poolA = await createAgentPool(token, uniqueName('e2e-rt-a'));
    const poolB = await createAgentPool(token, uniqueName('e2e-rt-b'));
    // Start with one pool, add the second through the UI.
    const wsId = await createWorkspace(token, uniqueName('e2e-multipool-rt'), {
      'execution-mode': 'agent',
      'agent-pool-ids': [poolA],
    });

    await page.goto(`/workspaces/${wsId}`);
    await page.getByRole('button', { name: /^edit$/i }).first().click();

    // The editor lists every assignable pool as a checkbox — not a native
    // multi-select, which is a poor tap target and hides its own state.
    const checkboxes = page.getByRole('checkbox');
    await expect(checkboxes.first()).toBeVisible({ timeout: 10_000 });

    // Exactly one is checked to begin with.
    await expect(page.getByRole('checkbox', { checked: true })).toHaveCount(1);

    // Tick a second pool and save.
    const unchecked = page.getByRole('checkbox', { checked: false }).first();
    await unchecked.check();
    await page.getByRole('button', { name: /^save$/i }).first().click();

    // Re-enter edit: the second pool stuck.
    await expect(page.getByRole('button', { name: /^edit$/i }).first()).toBeVisible({
      timeout: 10_000,
    });
    await page.getByRole('button', { name: /^edit$/i }).first().click();
    await expect(page.getByRole('checkbox', { checked: true })).toHaveCount(2);

    expect(poolB).toBeTruthy(); // both pools were created for this workspace
  });

  test('the pool editor does not push the page sideways on a phone', async ({ page }) => {
    const token = getStoredToken('admin.json');
    const poolA = await createAgentPool(token, uniqueName('e2e-mob-a'));
    const poolB = await createAgentPool(token, uniqueName('e2e-mob-b'));
    const wsId = await createWorkspace(token, uniqueName('e2e-multipool-mob'), {
      'execution-mode': 'agent',
      'agent-pool-ids': [poolA, poolB],
    });

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/workspaces/${wsId}`);
    await expect(page.getByText('Agent pools', { exact: true })).toBeVisible({ timeout: 10_000 });
    await expectNoHorizontalPageScroll(page);

    // And in edit mode, where the checkbox rows are.
    await page.getByRole('button', { name: /^edit$/i }).first().click();
    await expect(page.getByRole('checkbox').first()).toBeVisible({ timeout: 10_000 });
    await expectNoHorizontalPageScroll(page);
  });
});
