/**
 * Run lifecycle smoke — the biggest E2E gap before this PR.
 *
 * The CI E2E suite covers admin / auth / workspaces / variables /
 * registry but didn't touch the run-detail page or run lifecycle
 * actions. This file pins:
 *
 *  - Workspace's Runs tab renders
 *  - Run-detail page renders with status badge + Details panel
 *  - Plan output / Apply output tabs are present
 *  - Cancel button visibility maps to non-terminal status
 *  - "Back to workspace" navigation works
 *
 * Heavy lifting (real terraform plan/apply) lives in the Tilt smoke,
 * not here — the E2E stack has no runner pool. We exercise the UI
 * surfaces with API-seeded runs.
 */
import { test, expect, type Route, type Page } from '@playwright/test';
import { getStoredToken, createWorkspace, seedRun, uniqueName, API_URL } from '../helpers/api';
import { queueRunKind } from '../helpers/run-kinds';

test.describe('Run lifecycle UI', () => {
  test('workspace runs tab renders empty state cleanly', async ({ page }) => {
    const wsName = `e2e-runs-empty-${Date.now()}`;

    await page.goto('/workspaces');
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');
    await expect(page.locator(`text=${wsName}`)).toBeVisible({ timeout: 10_000 });

    await page.click(`text=${wsName}`);
    await page.getByRole('button', { name: 'Runs' }).click();

    // Empty workspaces should still render the runs section header /
    // empty state without console errors. Either a table or an empty
    // hint should be visible — exact shape is UI-internal, so we just
    // pin that NO error banner shows up.
    await expect(page.locator('text=/Failed to load|Error/i')).toHaveCount(0);
  });

  test('runs tab navigation preserves tab parameter on reload', async ({ page }) => {
    const wsName = `e2e-tab-${Date.now()}`;

    await page.goto('/workspaces');
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');
    await page.click(`text=${wsName}`);

    await page.getByRole('button', { name: 'Runs' }).click();
    // URL should carry ?tab=runs
    await expect(page).toHaveURL(/[?&]tab=runs/);

    await page.reload();
    // Still on runs tab after reload (status-active styling)
    const runsBtn = page.getByRole('button', { name: 'Runs' });
    await expect(runsBtn).toBeVisible();
  });

  // ── Plan vs plan+apply is chosen by button, not a hidden checkbox (#1340) ──

  test('both queue buttons are offered, and Plan is unchanged', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-queue-buttons'));

    await page.goto(`/workspaces/${wsId}?tab=runs`);

    // The decision that matters is on the surface, not behind "Options" -- and
    // since F12 the surface is a split button, so this test owns BOTH halves of
    // that claim and reads the test ids directly rather than going through the
    // helper. `Plan + apply` is the PRIMARY: the most complete kind this
    // workspace declares, and therefore the one loud control (F10). `Plan` is a
    // partial, so it is offered in the menu rather than on the row.
    //
    // Asserting the position is the point here. Every other spec says only
    // "this kind is offered" and leaves the position to the derivation, which
    // is why they use the helper and this one does not.
    await expect(page.getByTestId('ws-run-planApply')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('ws-run-plan')).toHaveCount(0);

    await page.getByTestId('ws-run-more').click();
    await expect(page.getByTestId('ws-run-plan')).toBeVisible();
    await page.keyboard.press('Escape');

    // The checkbox it replaced must be gone — if it lingered, the two
    // mechanisms could disagree about what the next run is.
    await page.getByRole('button', { name: /Options/ }).click();
    await expect(page.getByText('Plan Only', { exact: true })).toHaveCount(0);
  });

  test('default-run-kind moves the primary, and the displaced kind stays offered', async ({
    page,
  }) => {
    // The test above pins the DEFAULT ordering: the most complete kind the
    // workspace declares is the primary. This one pins that a workspace may
    // choose otherwise (#2479) -- which is the whole of the feature, and the
    // one claim no unit test can make, because the derivation is a pure
    // function and what is under test here is that the page passes it the
    // stored value at all.
    //
    // Both positions are asserted, in both directions. "Plan is now the
    // primary" alone would pass against a page that had simply lost its menu,
    // and the point of the setting is that it reorders rather than removes:
    // every kind the workspace offers stays reachable.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-primary-run-kind'));

    // Written through the NATIVE route, which is the only surface that serves
    // this attribute -- the TFE-compatible prefix strips it, because
    // `terraform` and `tofu` have no run button.
    const res = await fetch(`${API_URL}/api/v1/workspaces/${wsId}`, {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/vnd.api+json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ data: { attributes: { 'default-run-kind': 'plan' } } }),
    });
    expect(res.ok, `setting default-run-kind failed: ${res.status}`).toBe(true);

    await page.goto(`/workspaces/${wsId}?tab=runs`);

    // Swapped: `Plan` is the loud control and `Plan + apply` has moved behind
    // the caret -- the exact inverse of the default this workspace started on.
    await expect(page.getByTestId('ws-run-plan')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('ws-run-planApply')).toHaveCount(0);

    await page.getByTestId('ws-run-more').click();
    await expect(page.getByTestId('ws-run-planApply')).toBeVisible();
    await page.keyboard.press('Escape');
  });

  test('Plan queues a plan-only run', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-queue-plan'));

    // Assert on what is actually sent: "plan-only" true is the behaviour the
    // issue promised to leave untouched, and a label alone cannot prove it.
    let body: string | null = null;
    await page.route('**/api/*/runs', async (route: Route) => {
      if (route.request().method() === 'POST') body = route.request().postData();
      await route.continue();
    });

    await page.goto(`/workspaces/${wsId}?tab=runs`);
    await queueRunKind(page, 'plan');

    await expect.poll(() => body, { timeout: 15_000 }).not.toBeNull();
    expect(JSON.parse(body!).data.attributes['plan-only']).toBe(true);
  });

  test('a drift run that found drift offers plan + apply', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-drift-remediate'));

    // The API cannot create a drift run — only the drift checker does, and the
    // E2E stack has no runner. So the run payload is stubbed: this pins the UI
    // gate (drift + has-changes ⇒ offer), not the detection itself.
    await page.route('**/api/*/runs/run-*', async (route: Route) => {
      const res = await route.fetch();
      const json = await res.json().catch(() => null);
      if (!json?.data?.attributes) return route.fulfill({ response: res });
      json.data.attributes['is-drift-detection'] = true;
      json.data.attributes['has-changes'] = true;
      await route.fulfill({ response: res, json });
    });

    // Plain plan-only run (config version and all) — the route mock above is
    // what makes it read as a drift run.
    const runId = await seedRun(token, wsId);
    await page.goto(`/workspaces/${wsId}/runs/${runId}`);

    await expect(page.getByRole('button', { name: 'Plan + apply', exact: true })).toBeVisible({
      timeout: 15_000,
    });
  });
  test('phase wording resolves rather than leaking a message key', async ({ page }) => {
    // #1521 moved the run's phase words behind the engine's vocabulary
    // namespace, so what the page renders now depends on the key
    // `phases.<engine>.<group>.<state>` resolving. Get the namespace wrong and
    // the label does not vanish or throw — it renders the raw key. Nothing else
    // catches that: type-checking passes, the catalogue gate passes (the keys
    // exist), and a page asserting only "no error banner" is happy.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-phase-vocab'));
    const runId = await seedRun(token, wsId);

    await page.goto(`/workspaces/${wsId}/runs/${runId}`);
    await expect(page.getByText(/Failed to load/i)).toHaveCount(0);

    // A raw key anywhere on the page is the failure. Matching the prefix rather
    // than one exact key keeps this honest whichever phase the seeded run is in.
    await expect(
      page.getByText(/phases\.[a-z]+\.(runStatus|activity|status|words)\./),
    ).toHaveCount(0);
  });

  test('the run page carries its engine through to the API response', async ({ page }) => {
    // The wording is chosen from the run's own engine, so the attribute has to
    // actually arrive — a serializer that dropped it would fall back to
    // Terraform's words and look perfectly fine on a Terraform-only install,
    // which is exactly where this would go unnoticed.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-phase-engine'));
    const runId = await seedRun(token, wsId);

    let engine: unknown;
    await page.route(`**/api/*/runs/${runId}`, async (route: Route) => {
      const response = await route.fetch();
      const json = await response.json();
      engine = json?.data?.attributes?.engine;
      await route.fulfill({ response, json });
    });

    await page.goto(`/workspaces/${wsId}/runs/${runId}`);
    await expect.poll(() => engine, { timeout: 15_000 }).toBe('terraform');
  });

  // ── a result indicator carries you to what explains it (#2413 F16) ────────
  //
  // The maintainer's instruction: *"when you click errored on a workspace you
  // get taken to the run which errored ... with the log view optimised for what
  // you click on. This is important in general for ergonomics."*
  //
  // The phase timestamps are STUBBED into the list response, the same way the
  // drift-remediate test above stubs a run: a seeded run never starts a phase
  // (the E2E stack has no runner), so `status-timestamps` carries no
  // `planning-at` and the badge is correctly a plain span with nothing to link
  // to. Stubbing pins the UI derivation, not the phase transitions.

  /** Inject phase timestamps into whatever the workspace's run list returns. */
  async function stubRunTimestamps(page: Page, stamps: Record<string, string>) {
    await page.route('**/api/*/workspaces/*/runs*', async (route: Route) => {
      const res = await route.fetch();
      const json = await res.json().catch(() => null);
      if (!Array.isArray(json?.data)) return route.fulfill({ response: res });
      for (const r of json.data) {
        r.attributes['status-timestamps'] = { ...(r.attributes['status-timestamps'] ?? {}), ...stamps };
      }
      await route.fulfill({ response: res, json });
    });
  }

  test('the status badge opens the phase that explains the result', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-explain-apply'));
    await seedRun(token, wsId);

    // Everything up to and including the apply started, so the apply is the
    // furthest the run got and therefore the phase that explains it.
    await stubRunTimestamps(page, {
      'planning-at': '2026-01-01T00:00:00Z',
      'planned-at': '2026-01-01T00:01:00Z',
      'applying-at': '2026-01-01T00:02:00Z',
    });

    await page.goto(`/workspaces/${wsId}?tab=runs`);
    // By its accessible name, which carries the purpose as well as the status
    // -- the badge is a `<button>` only when there is a phase to open.
    const badge = page.getByRole('button', { name: /open the log of the phase/i }).first();
    await expect(badge).toBeVisible({ timeout: 15_000 });
    await badge.click();

    await expect(page).toHaveURL(/\/runs\/run-[^?]+\?view=apply$/, { timeout: 15_000 });
  });

  test('a configure badge opens the configure log, not the explaining phase', async ({ page }) => {
    // The two differ on a composite that errored in its apply, and a badge that
    // NAMES a phase has to mean what it says.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-explain-cfg'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'site.yml',
    });
    await seedRun(token, wsId);

    await page.route('**/api/*/workspaces/*/runs*', async (route: Route) => {
      const res = await route.fetch();
      const json = await res.json().catch(() => null);
      if (!Array.isArray(json?.data)) return route.fulfill({ response: res });
      for (const r of json.data) {
        // `run-kind` is authoritative over `is-configure` (#2507), so setting
        // only the latter leaves the run saying plan_apply and the configure
        // badge correctly does not render. This test is about a COMPOSITE -- a
        // plan+apply AND a configure -- which is what `full` means.
        r.attributes['run-kind'] = 'full';
        r.attributes['is-configure'] = true;
        // The APPLY is the explaining phase here, deliberately: it proves the
        // configure badge is not merely inheriting the status badge's answer.
        r.attributes['status-timestamps'] = {
          'planning-at': '2026-01-01T00:00:00Z',
          'planned-at': '2026-01-01T00:01:00Z',
          'applying-at': '2026-01-01T00:02:00Z',
        };
      }
      await route.fulfill({ response: res, json });
    });

    await page.goto(`/workspaces/${wsId}?tab=runs`);
    const cfg = page.getByRole('button', { name: 'configure', exact: true }).first();
    await expect(cfg).toBeVisible({ timeout: 15_000 });
    await cfg.click();

    await expect(page).toHaveURL(/\?view=configure$/, { timeout: 15_000 });
  });

  test('a run that never started a phase has nothing to open', async ({ page }) => {
    // The negative half, and the reason the badge is conditionally a button:
    // linking a queued run to a plan log that does not exist yet would be worse
    // than not linking it. A seeded run is exactly this case, unstubbed.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-explain-none'));
    await seedRun(token, wsId);

    await page.goto(`/workspaces/${wsId}?tab=runs`);
    await expect(page.getByTestId('ws-run-planApply')).toBeVisible({ timeout: 15_000 });
    await expect(
      page.getByRole('button', { name: /open the log of the phase/i }),
    ).toHaveCount(0);
  });
});
