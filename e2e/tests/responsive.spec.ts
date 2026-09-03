import { test, expect, type Page, type Route } from '@playwright/test';
// Lives in helpers/, not here: Playwright forbids a spec importing a spec, and
// any suite adding a surface should be able to reuse the mobile guard.
import { expectNoHorizontalPageScroll } from '../helpers/responsive';
import { getStoredToken, createWorkspace, createUser, createAgentPool, createRegistryModule, seedRun, seedStateVersion, seedStateVersionWithContent, seedRunTask, uniqueName } from '../helpers/api';

const API_URL = process.env.API_URL || 'http://localhost:8000';

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

test.describe('Responsive harness (phone viewport)', () => {
  test('the Vault reference builder is usable at phone width (#1439)', async ({ page }) => {
    // The desktop defect this guards against in the other direction: five
    // fields crammed into a narrow container truncated the path and field
    // inputs to unreadable stubs. The same component renders in the mobile
    // card, so it has to hold up here too.
    const token = getStoredToken()
    // Agent mode: a vault reference only resolves on the listener claim path,
    // so the source picker is not offered on a local workspace.
    const wsId = await createWorkspace(token, uniqueName('e2erespvault'), {
      'execution-mode': 'agent',
    })

    await page.route('**/api/terrapod/v1/vault/availability', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: {
            type: 'vault-availability',
            id: 'vault',
            attributes: { enabled: true, instances: ['default'], 'default-instance': 'default' },
          },
        }),
      }),
    )

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-source').selectOption('vault')

    // Every coordinate field must be reachable and typable, not clipped.
    for (const id of ['#add-mount', '#add-path', '#add-field']) {
      await expect(page.locator(id)).toBeVisible()
    }
    await page.locator('#add-path').fill('apps/some/deeper/path')
    await expect(page.locator('#add-path')).toHaveValue('apps/some/deeper/path')
    await expectNoHorizontalPageScroll(page)
  })

  test('workspace variable sets panel adapts to mobile (#1440)', async ({ page }) => {
    // Seeded rather than asserted on an empty page: with no set applying, the
    // panel renders nothing at all and the assertion would pass however the
    // layout is written.
    const token = getStoredToken()
    const wsName = uniqueName('e2erespvs')
    const wsId = await createWorkspace(token, wsName, { labels: { e2erespvs: wsName } })
    const res = await fetch(`${API_URL}/api/v2/organizations/default/varsets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: {
          attributes: {
            name: uniqueName('e2erespset'),
            'assignment-rule': { labels: { e2erespvs: wsName } },
          },
        },
      }),
    })
    expect(res.status).toBe(201)
    const vsName = (await res.json()).data.attributes.name

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    const entry = page.locator('li').filter({ hasText: vsName })
    await expect(entry).toBeVisible({ timeout: 15_000 })
    // The set name and its source badge both have to survive at phone width —
    // the source is the primary signal here, not decoration.
    await expect(entry.getByText('Matched by rule')).toBeVisible()
    await expectNoHorizontalPageScroll(page)
  })

  test('deleted-workspaces admin page adapts to mobile (#1253)', async ({ page }) => {
    // Seed a real deleted workspace first. Asserting the table is hidden on an
    // EMPTY page proves nothing — with no rows the component renders an empty
    // state and there is no table in the DOM at all, so the assertion passes
    // however the breakpoints are written.
    const token = getStoredToken()
    const name = uniqueName('e2eresp')
    const wsId = await createWorkspace(token, name)
    await fetch(`${API_URL}/api/terrapod/v1/workspaces/${wsId}`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${token}` },
    })

    await page.goto('/admin/deleted-workspaces')
    // Scoped to the card: the name appears in BOTH renders (the desktop table
    // is in the DOM, just hidden), so an unscoped getByText is ambiguous —
    // which is itself evidence the dual-render is present.
    const card = page.locator('ul > li').filter({ hasText: name })
    await expect(card).toBeVisible({ timeout: 10_000 })
    await expectNoHorizontalPageScroll(page)

    // Now the assertion bites: rows exist, so the desktop table is present in
    // the tree and must be hidden by width, with the card list rendering in
    // its place. One component driven by the breakpoint, not a forked build.
    await expect(page.locator('table')).toBeHidden()
  })

  test('VCS connection consumption renders at phone width (#1339)', async ({ page }) => {
    // Seed a real connection first. On an empty page the table is not in the
    // DOM at all, so the assertion would pass however the breakpoints are
    // written — the same trap as the deleted-workspaces test above.
    const token = getStoredToken()
    const name = uniqueName('e2eresp-vcs')
    const created = await fetch(`${API_URL}/api/terrapod/v1/vcs-connections`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/vnd.api+json' },
      body: JSON.stringify({
        data: { type: 'vcs-connections', attributes: { name, provider: 'gitlab', token: 'glpat-e2e-not-a-real-token' } },
      }),
    })
    expect(created.ok).toBeTruthy()
    const connId = (await created.json()).data.id

    try {
      await page.goto('/admin/vcs-connections')
      await expect(page.getByText(name)).toBeVisible({ timeout: 15_000 })

      // A freshly created connection has made no calls and the server has
      // reported no budget, so there is nothing to classify: it reads **Not
      // reported**.
      //
      // This assertion previously expected "Idle", which is what the code did
      // and exactly the defect (#1345): a verdict was fabricated from the
      // absence of any reading, so a GitLab connection being polled hard —
      // GitLab was not instrumented at all — rendered a calm grey badge while
      // the runbook sent the operator here to diagnose stalled runs. A verdict
      // is now withheld unless there is a budget to judge against; the rate is
      // still reported, because that is Terrapod's own tally.
      await expect(page.getByText('Not reported').first()).toBeVisible({ timeout: 15_000 })

      // The connection renders as a panel (#1339) rather than a table row, so
      // it reflows to a single column here — nothing may push the page sideways.
      await expectNoHorizontalPageScroll(page)
    } finally {
      await fetch(`${API_URL}/api/terrapod/v1/vcs-connections/${connId}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
    }
  })

  test('both queue buttons are reachable at phone width (#1340)', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2eresp-queue'))

    await page.goto(`/workspaces/${wsId}?tab=runs`)

    // Plan-vs-apply is the decision that matters every time, so neither button
    // may be hidden behind a breakpoint — a phone must be able to make the
    // same choice a desktop can.
    await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByRole('button', { name: 'Plan + apply', exact: true })).toBeVisible()

    // Four buttons on one row is where a phone starts scrolling sideways.
    await expectNoHorizontalPageScroll(page)
  })

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
    //
    // The nav is a client component: right after navigation the hamburger is
    // SSR-rendered and visible, but a click can land BEFORE React hydrates and
    // wires its onClick — the click is swallowed, `menuOpen` never flips, and
    // #mobile-nav-menu never mounts (the pre-hydration lost-click flake, #902).
    // Retry the click until the sheet actually opens, clicking only while it's
    // still closed so a late-hydrated handler can't toggle it back shut.
    const menu = page.locator('#mobile-nav-menu');
    await expect(async () => {
      if (!(await menu.isVisible())) await hamburger.click();
      await expect(menu).toBeVisible({ timeout: 1000 });
    }).toPass({ timeout: 15000 });
    // exact: the Admin group also carries a "Deleted workspaces" link (#1253),
    // which a substring match would pick up as a second element.
    await expect(menu.getByRole('link', { name: 'Workspaces', exact: true })).toBeVisible();
    await expect(menu.getByText('Registry', { exact: true })).toBeVisible();
    await expect(menu.getByText('Help', { exact: true })).toBeVisible();
    await expect(menu.getByRole('link', { name: 'Modules' })).toBeVisible();
    await expect(menu.getByText('Account', { exact: true })).toHaveCount(0);
    // Opening the sheet must not introduce horizontal overflow.
    await expectNoHorizontalPageScroll(page);

    // Account has its own trigger + drawer (personal/session items + log out).
    await menu.getByRole('button', { name: /close menu/i }).click();
    // Wait for the nav sheet to fully close before opening the account drawer.
    // The open sheet is a full-screen `fixed … z-40` overlay covering the account
    // trigger in the top bar; closing it unmounts the drawer via a React state
    // update. Driving the account-open click before that unmount completes lets
    // the click miss (the overlay still intercepts) so the account drawer never
    // opens — the intermittent "element(s) not found" flake (#896). Gate on the
    // sheet being gone, then on the trigger being actionable, before clicking.
    await expect(menu).toBeHidden();
    const accountTrigger = page.getByRole('button', { name: 'Open account menu' });
    await expect(accountTrigger).toBeVisible();
    await accountTrigger.click();
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

  test('workspace cost tab has no horizontal page scroll at phone width', async ({ page }) => {
    // The Cost tab (#871) — headline card + per-resource table (which collapses
    // to stacked cards below sm). A fresh workspace has no state, so it shows
    // the deterministic empty state; either way the page must not h-scroll.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('resp-cost'));
    await page.goto(`/workspaces/${wsId}?tab=cost`);
    await expect(page.getByText(/No cost yet/i)).toBeVisible({ timeout: 15_000 });
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

  test('workspace state graph defaults to the accessible table at phone width', async ({ page }) => {
    // The state resource graph (#765) is WebGL-heavy and desktop-oriented, so a
    // phone defaults to the accessible table (via useIsMobile) — never a blank
    // canvas — and must not scroll horizontally.
    const token = getStoredToken();
    const wsName = uniqueName('resp-stategraph');
    const wsId = await createWorkspace(token, wsName);
    await seedStateVersionWithContent(token, wsId, [
      { mode: 'managed', type: 'null_resource', name: 'hub', instances: [{ dependencies: [] }] },
    ]);

    await page.goto(`/workspaces/${wsId}?tab=state-graph`);

    // Phone → Table view is the default: the resource is listed as a rowheader.
    await expect(page.getByRole('rowheader', { name: 'null_resource.hub' })).toBeVisible({ timeout: 15_000 });
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

    await page.goto(`/workspaces/${wsId}?tab=versions`);

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

  test('estate topology defaults to the table at phone width (#763)', async ({ page }) => {
    // On a phone the estate page defaults to the accessible Table view rather
    // than heavy WebGL (#736 a11y + #719 mobile). Assert the table renders and
    // the page does not scroll horizontally.
    const token = getStoredToken()
    const wsName = uniqueName('e2e-estate-mob')
    await createWorkspace(token, wsName, { labels: { team: 'estate-mob' } })

    await page.goto('/estate')
    await expect(page.getByRole('heading', { name: 'Estate topology', level: 1 })).toBeVisible({
      timeout: 15_000,
    })
    // Phone default is the table — its Workspaces heading is visible without a toggle.
    await expect(page.getByRole('columnheader', { name: 'Workspace' })).toBeVisible({
      timeout: 10_000,
    })
    await expectNoHorizontalPageScroll(page)
  })

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

/**
 * Tablet-width guard for the md–lg dead-zone (#839).
 *
 * The phone `responsive` project runs below md and the desktop projects run
 * well above lg, so the 768–1023px band — where the desktop nav used to render
 * (at md) but not fit until lg, wrapping into a tall sticky bar that shoved page
 * content (incl. the run/workspace tab bar) out of view — was tested by NEITHER
 * side. This block pins that band: the nav must still be its compact hamburger
 * form (desktop link row is `hidden lg:flex`, so it must NOT show here), and no
 * page must scroll horizontally — including the ≤11-tab workspace-detail strip,
 * which now scrolls within itself (`overflow-x-auto`) instead of overflowing the
 * page. One DRY viewport-driven UI, verified at the seam.
 */
test.describe('Tablet width (md–lg dead-zone, #839)', () => {
  test.use({ viewport: { width: 900, height: 900 }, isMobile: false });

  test('nav shows the icon bar, not the hamburger, no page h-scroll', async ({ page }) => {
    await page.goto('/workspaces');
    // #839 resolved this width by showing the hamburger; #1400 reversed that.
    // The icon-only bar needs about 650px, so hiding it here hid a bar that
    // fits. The nav now switches at md, where the rest of the UI switches to
    // its phone treatment, so 900px is squarely desktop and gets the bar.
    await expect(page.getByRole('button', { name: /open menu/i })).toBeHidden();
    await expect(
      page.getByRole('navigation').getByRole('link', { name: 'Workspaces', exact: true }),
    ).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });

  test('workspace-detail tab strip scrolls within itself, no page h-scroll', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-tabstrip-tablet'));

    await page.goto(`/workspaces/${wsId}`);
    // At ≥md the desktop tab bar renders (not the mobile <select>).
    await expect(page.getByRole('button', { name: 'Configuration' })).toBeVisible({ timeout: 15_000 });
    // The ~11-tab strip must not push the page into horizontal scroll — it is
    // contained by overflow-x-auto on its wrapper (the #839 fix).
    await expectNoHorizontalPageScroll(page);
  });

})

test.describe('Role reach panel (#1456)', () => {
  test('the reach panel fits a phone without scrolling the page sideways', async ({ page }) => {
    await page.goto('/admin/roles');
    await expectNoHorizontalPageScroll(page);

    await page.getByRole('button', { name: /create role/i }).click();
    const panel = page.getByTestId('role-reach');
    await expect(panel).toBeVisible();

    // Workspace names and label rules are both unbounded strings, so this is
    // the panel most likely to push a phone layout sideways.
    await page.fill('#r-allow-labels', 'env=production-eu-west-1-primary');
    await expectNoHorizontalPageScroll(page);
    await expect(panel).toBeVisible();
  });
});
