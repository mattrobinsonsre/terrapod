import { test, expect, type Page } from '@playwright/test';
import { getStoredToken, createWorkspace, createUser, createAgentPool, createRegistryModule, seedRun, seedStateVersion, seedRunTask, uniqueName } from '../helpers/api';

/**
 * Responsive / mobile harness (#719).
 *
 * This project runs at a phone viewport (see the `responsive` project in
 * playwright.config.ts — a Pixel device descriptor). It is the "mobile"
 * half of the two-sided testing contract: this suite proves the UI works
 * at phone width, while the existing desktop projects prove the desktop
 * view is unchanged (the desktop guard). One DRY UI, adapted by width —
 * never a forked mobile build, never user-agent sniffing.
 *
 * Per-page assertions (no horizontal page scroll, tables reflow, tab
 * survives reload, log tail visible, …) are added to this suite as each
 * stage of #719 fixes the corresponding surface, so the guard grows with
 * the work and can't silently regress.
 */

/**
 * Asserts the page does not scroll horizontally at the current viewport —
 * the single most important mobile invariant. Allows a 1px rounding slack.
 */
export async function expectNoHorizontalPageScroll(page: Page) {
  const overflow = await page.evaluate(() => {
    const el = document.documentElement;
    return el.scrollWidth - el.clientWidth;
  });
  expect(
    overflow,
    `page scrolls horizontally by ${overflow}px at ${page.viewportSize()?.width}px viewport`,
  ).toBeLessThanOrEqual(1);
}

test.describe('Responsive harness (phone viewport)', () => {
  test('runs at a phone viewport', async ({ page }) => {
    const vp = page.viewportSize();
    expect(vp, 'responsive project must set a viewport').not.toBeNull();
    expect(vp!.width, 'responsive project runs below the md breakpoint').toBeLessThan(768);
  });

  test('nav adapts to mobile: hamburger shown, grouped sheet', async ({ page }) => {
    await page.goto('/workspaces');
    await expectNoHorizontalPageScroll(page);

    // The mobile branch of the nav renders a hamburger toggle; the desktop
    // link row is hidden below md. Proves the single nav component adapts
    // by width — no forked mobile build (#719).
    const hamburger = page.getByRole('button', { name: /open menu/i });
    await expect(hamburger).toBeVisible();

    // Opening it reveals the grouped sheet: primary links plus labelled
    // sections (Registry / Help, + Admin for admins). Account is NOT here — it
    // has its own trigger + drawer.
    await hamburger.click();
    const menu = page.locator('#mobile-nav-menu');
    await expect(menu).toBeVisible();
    await expect(menu.getByRole('link', { name: 'Workspaces' })).toBeVisible();
    await expect(menu.getByText('Registry', { exact: true })).toBeVisible();
    await expect(menu.getByText('Help', { exact: true })).toBeVisible();
    await expect(menu.getByRole('link', { name: 'Modules' })).toBeVisible();
    await expect(menu.getByText('Account', { exact: true })).toHaveCount(0);
    // Opening the sheet must not introduce horizontal overflow.
    await expectNoHorizontalPageScroll(page);

    // Account has its own trigger + drawer (personal/session items + log out).
    await menu.getByRole('button', { name: /close menu/i }).click();
    await page.getByRole('button', { name: 'Open account menu' }).click();
    const account = page.locator('#mobile-account-menu');
    await expect(account).toBeVisible();
    await expect(account.getByRole('link', { name: 'API Tokens' })).toBeVisible();
    await expect(account.getByRole('button', { name: 'Log out' })).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });

  test('workspace list surfaces status in-row at phone width', async ({ page }) => {
    // Below `lg` the STATUS table column is hidden, so the row must carry an
    // inline status indicator — otherwise a phone loses the running/errored/
    // applied signal entirely (regression the mobile status line fixes, #719).
    const token = getStoredToken();
    const name = uniqueName('resp-status');
    await createWorkspace(token, name);

    // The client-side filter reads the `q` query param — narrow to our row.
    await page.goto(`/workspaces?q=${encodeURIComponent(name)}`);
    const row = page.getByRole('row').filter({ hasText: name });
    await expect(row).toBeVisible();

    // The inline mobile status indicator is present (a fresh workspace shows
    // "—", a run-bearing one shows its coloured pill — either way, not hidden).
    await expect(row.getByTestId('ws-row-status-mobile')).toBeVisible();

    // The dedicated desktop STATUS column header stays hidden at this width.
    await expect(page.getByRole('columnheader', { name: 'Status' })).toBeHidden();

    await expectNoHorizontalPageScroll(page);
  });

  test('workspace list trims secondary chrome at phone width', async ({ page }) => {
    // On a phone we drop the explanatory subtitle and the Total/Locked
    // stat cards (secondary), but KEEP Health Issues (the primary signal
    // that something needs attention) — #719.
    await page.goto('/workspaces');
    await expect(page.getByRole('heading', { name: 'Workspaces', level: 1 })).toBeVisible();

    await expect(page.getByText('Manage Terraform workspaces, state, and runs')).toBeHidden();
    // Compact stat chips: Total/Locked are desktop-only; Health always shows.
    await expect(page.getByText('Total', { exact: true })).toBeHidden();
    await expect(page.getByText('Locked', { exact: true })).toBeHidden();
    await expect(page.getByText('Health', { exact: true })).toBeVisible();

    await expectNoHorizontalPageScroll(page);
  });

  test('run detail page: native view picker drives navigation at phone width', async ({ page }) => {
    // The run-detail page is the hard mobile surface (#721/#722): the view
    // tabs collapse to a native <select> (no horizontal-scroll strip), the URL
    // stays the source of truth for the active view, and there is no horizontal
    // page scroll. Seed a run — the E2E stack has no runner so it sits `queued`,
    // which renders the whole page without needing real execution.
    const token = getStoredToken();
    const wsName = uniqueName('resp-run');
    const wsId = await createWorkspace(token, wsName);
    const runId = await seedRun(token, wsId);

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=overview`);
    await expect(
      page.getByRole('heading', { name: new RegExp(wsName), level: 1 }),
    ).toBeVisible({ timeout: 15_000 });

    // Below md the tabs are a native <select>, not a scrolling tab strip.
    const picker = page.locator('#run-view-select');
    await expect(picker).toBeVisible();
    await expectNoHorizontalPageScroll(page);

    // The picker is the source of truth for the active view — selecting an
    // option updates the URL (survives reload / back / deep-link).
    await picker.selectOption('plan');
    await expect(page).toHaveURL(/[?&]view=plan/);
    await expectNoHorizontalPageScroll(page);
  });

  test('workspace runs list becomes tappable cards at phone width', async ({ page }) => {
    // The 7-column runs table is unreadable on a phone, so below md it renders
    // as stacked cards driven by the same data (#719 Stage 2). The desktop
    // table header is hidden; the seeded run shows as a card that is itself a
    // link to the run (one big tap target).
    const token = getStoredToken();
    const wsName = uniqueName('resp-runs');
    const wsId = await createWorkspace(token, wsName);
    await seedRun(token, wsId);

    await page.goto(`/workspaces/${wsId}?tab=runs`);

    // The 9-tab strip collapses to a native <select> section picker at phone
    // width (the tab bar overflows a phone), driven by the same ?tab= URL.
    await expect(page.locator('#ws-tab-select')).toBeVisible();
    // The desktop table's column header is hidden at phone width...
    await expect(page.getByRole('columnheader', { name: 'Run ID' })).toBeHidden();
    // ...and the run renders as a card linking to the run detail page.
    await expect(page.locator('a[href*="/runs/run-"]').first()).toBeVisible({ timeout: 15_000 });

    await expectNoHorizontalPageScroll(page);
  });

  test('workspace state list becomes cards at phone width', async ({ page }) => {
    // The state-version table hid Created-by / Run / Size / Created behind
    // sm/md/lg breakpoints, leaving a phone with only the serial. Below md it
    // renders as cards driven by the same data (#719), so nothing is dropped.
    const token = getStoredToken();
    const wsName = uniqueName('resp-state');
    const wsId = await createWorkspace(token, wsName);
    await seedStateVersion(token, wsId, 1);

    await page.goto(`/workspaces/${wsId}?tab=state`);

    // The 9-tab strip is the native <select> picker at phone width.
    await expect(page.locator('#ws-tab-select')).toBeVisible();
    // The desktop table's Serial column header is hidden below md...
    await expect(page.getByRole('columnheader', { name: 'Serial' })).toBeHidden();
    // ...and the state version renders as a card with its serial and a Download
    // button. `#1` + Download also exist in the hidden desktop table, so filter
    // to the visible (mobile-card) copy.
    await expect(page.getByText('#1', { exact: true }).filter({ visible: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Download' }).filter({ visible: true })).toBeVisible();

    await expectNoHorizontalPageScroll(page);
  });

  test('workspace configurations list becomes cards at phone width', async ({ page }) => {
    // The 6-column configuration-versions table is 529px wide and was clipped
    // by its overflow-hidden wrapper on a phone (Created + Download vanished).
    // Below md it renders as cards driven by the same data (#719). Seeding a run
    // uploads a configuration version.
    const token = getStoredToken();
    const wsName = uniqueName('resp-cfg');
    const wsId = await createWorkspace(token, wsName);
    await seedRun(token, wsId);

    await page.goto(`/workspaces/${wsId}?tab=configurations`);

    await expect(page.locator('#ws-tab-select')).toBeVisible();
    // The desktop table's column header is hidden at phone width...
    await expect(page.getByRole('columnheader', { name: 'Source' })).toBeHidden();
    // ...and the config version renders as a card exposing its full id + the
    // Compare checkbox. Both also exist in the hidden desktop table, so filter
    // to the visible (mobile-card) copy.
    await expect(page.getByText(/^cv-/).filter({ visible: true }).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('checkbox', { name: /Select cv-.* for compare/ }).filter({ visible: true }).first()).toBeVisible();

    await expectNoHorizontalPageScroll(page);
  });

  test('admin users page fits a phone + delete/toggle are two-tier confirm buttons', async ({ page }) => {
    // Representative deep-admin surface (#719): the users table hides secondary
    // columns below breakpoints but keeps the Active/Inactive status in-row; the
    // row actions are real buttons; delete confirms in BOTH modes and the
    // activate/deactivate toggle confirms on touch (this Pixel project).
    const token = getStoredToken();
    const email = `${uniqueName('resp-user')}@example.com`;
    await createUser(token, email, 'Sup3rSecret!pw', 'Resp User');

    await page.goto('/admin/users');
    const row = page.getByRole('row').filter({ hasText: email });
    await expect(row).toBeVisible({ timeout: 15_000 });
    // Status stays visible at phone width (not hidden behind a breakpoint).
    await expect(row.getByRole('button', { name: /Active|Inactive/ })).toBeVisible();
    // Row actions are real buttons (Delete present as a button, not bare text).
    await expect(row.getByRole('button', { name: 'Delete' })).toBeVisible();
    await expectNoHorizontalPageScroll(page);

    // Tier-1 delete prompts on touch (and would on desktop too); dismiss keeps the row.
    let deleteMsg = '';
    page.once('dialog', async (d) => { deleteMsg = d.message(); await d.dismiss(); });
    await row.getByRole('button', { name: 'Delete' }).click();
    await expect.poll(() => deleteMsg, { timeout: 5_000 }).toContain('Delete user');
    await expect(row).toBeVisible();

    // Tier-2 activate/deactivate toggle prompts on touch; dismiss keeps state.
    let toggleMsg = '';
    page.once('dialog', async (d) => { toggleMsg = d.message(); await d.dismiss(); });
    await row.getByRole('button', { name: /Active|Inactive/ }).click();
    await expect.poll(() => toggleMsg, { timeout: 5_000 }).toMatch(/Deactivate|Activate/);
  });

  test('agent pools list + detail fit a phone viewport', async ({ page }) => {
    // Agent Pools is a top-level admin surface (#719). The list hides the
    // STATUS column below md, so the pool's health dot must reflow inline into
    // the row; the detail page (settings + tokens + listeners tables) must not
    // introduce horizontal page scroll.
    const token = getStoredToken();
    const poolName = uniqueName('resp-pool');
    const poolId = await createAgentPool(token, poolName);

    await page.goto('/admin/agent-pools');
    const row = page.getByRole('row').filter({ hasText: poolName });
    await expect(row).toBeVisible({ timeout: 15_000 });
    // The dedicated desktop STATUS column header stays hidden at phone width.
    await expect(page.getByRole('columnheader', { name: 'Status' })).toBeHidden();
    await expectNoHorizontalPageScroll(page);

    await page.goto(`/admin/agent-pools/${poolId}`);
    await expect(
      page.getByRole('heading', { name: new RegExp(poolName), level: 1 }),
    ).toBeVisible({ timeout: 15_000 });
    await expectNoHorizontalPageScroll(page);
  });

  test('touch: both a reversible toggle and an irreversible delete prompt confirm()', async ({ page }) => {
    // #719 two-tier confirm policy, coarse-pointer half. On touch EVERY mutation
    // prompts: tier-2 (toggle) — which on a precise pointer would NOT — and
    // tier-1 (delete). This Pixel project is the only proof of the touch path,
    // since the maintainer doesn't test on a real device.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('confirm-touch'));
    const rtName = uniqueName('rt');
    await seedRunTask(token, wsId, rtName);

    await page.goto(`/workspaces/${wsId}?tab=run-tasks`);
    await expect(page.getByText(rtName)).toBeVisible({ timeout: 15_000 });

    // Handlers registered BEFORE the click: window.confirm() is synchronous and
    // blocks the click handler, so the dialog must be handled as it opens
    // (waitForEvent + click deadlocks).

    // Tier 2 — the Disable toggle DOES prompt on touch; dismiss keeps it enabled.
    let toggleMsg = '';
    page.once('dialog', async (d) => { toggleMsg = d.message(); await d.dismiss(); });
    await page.getByRole('button', { name: 'Disable' }).click();
    await expect.poll(() => toggleMsg, { timeout: 5_000 }).toContain('Disable this run task');
    await expect(page.getByText('Enabled', { exact: true })).toBeVisible();

    // Tier 1 — delete prompts on touch too; dismiss keeps the row.
    let deleteMsg = '';
    page.once('dialog', async (d) => { deleteMsg = d.message(); await d.dismiss(); });
    await page.getByRole('button', { name: 'Delete' }).click();
    await expect.poll(() => deleteMsg, { timeout: 5_000 }).toContain('Delete run task');
    await expect(page.getByText(rtName)).toBeVisible();
  });

  test('catalog browse page renders without horizontal scroll at phone width', async ({ page }) => {
    // The catalog browse page is a responsive card grid (or an empty state);
    // either way it must not scroll horizontally on a phone.
    await page.goto('/catalog');
    await expect(page.getByRole('heading', { name: 'Service Catalog' })).toBeVisible({ timeout: 15_000 });
    await expectNoHorizontalPageScroll(page);
  });

  test('registry module list renders as a card grid at phone width', async ({ page }) => {
    // The registry list pages are responsive card grids (grid-cols-1 at phone),
    // so a seeded module shows as a full-width card with no horizontal scroll.
    const token = getStoredToken();
    const modName = uniqueName('respmod').replace(/[^a-z0-9]/gi, '');
    await createRegistryModule(token, modName, 'aws');

    await page.goto('/registry/modules');
    await expect(page.getByText(modName).first()).toBeVisible({ timeout: 15_000 });

    await expectNoHorizontalPageScroll(page);
  });

  test('impact graph is gated in the mobile view picker (#761)', async ({ page }) => {
    // The Impact graph (#761) lives on the run-detail page; per #719 the gating
    // must hold on mobile too. The E2E stack has no runner, so a seeded run has no
    // plan JSON output — the WebGL graph is unreachable (verified on the live Tilt
    // stack; its overlay panels are viewport-capped). Here we guard the mobile
    // surface: the run page fits the phone viewport and the native view picker
    // offers NO Impact option without plan JSON output.
    const token = getStoredToken();
    const wsName = uniqueName('e2e-impact-mob');
    const wsId = await createWorkspace(token, wsName);
    const runId = await seedRun(token, wsId, true);

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=overview`);
    const picker = page.locator('#run-view-select');
    await expect(picker).toBeVisible({ timeout: 15_000 });
    await expect(picker.locator('option[value="impact"]')).toHaveCount(0);
    await expectNoHorizontalPageScroll(page);
  });
});
