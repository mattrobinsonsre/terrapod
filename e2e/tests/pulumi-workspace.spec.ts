/**
 * A Pulumi workspace can be found, opened and edited (#1554).
 *
 * It could be created on the native surface but read, listed and edited only on
 * the TFE one, which serves Terraform alone — so the list left it out, its page
 * said "not found", and its settings could never be saved. The page and the
 * list now load and save through the native routes. The E2E stack enables the
 * Pulumi engine, so this runs through the real BFF chain end to end.
 */
import { test, expect } from '@playwright/test';
import {
  API_URL,
  getStoredToken,
  createPulumiWorkspace,
  createWorkspace,
  lockWorkspace,
  seedPulumiRun,
  seedRun,
  uniqueName,
} from '../helpers/api';
import { expectNoHorizontalPageScroll } from '../helpers/responsive.js';

test.describe('Pulumi workspace', () => {
  test('appears in the list, opens, and saves an edit', async ({ page }) => {
    const token = getStoredToken();
    const name = `${uniqueName('e2e-pulumi')}::dev`;
    const wsId = await createPulumiWorkspace(token, name);

    // The list shows a stack name as "project / stack" (#1555). By link name,
    // not exact text: the link also holds the engine badge.
    await page.goto('/workspaces');
    await expect(
      page.getByRole('link', { name: new RegExp(name.replace('::', ' / ')) }).first(),
    ).toBeVisible({ timeout: 20_000 });

    await page.goto(`/workspaces/${wsId}`);
    await expect(page.getByText(name, { exact: true }).first()).toBeVisible({ timeout: 20_000 });

    // Edit a setting every engine has, and save it.
    await page.getByRole('button', { name: /^edit$/i }).first().click();
    // The page has several "Add" buttons; this one sits in the labels row,
    // beside the key and value inputs.
    const keyInput = page.getByPlaceholder('key', { exact: true });
    await keyInput.fill('team');
    await page.getByPlaceholder('value', { exact: true }).fill('stacks');
    await keyInput.locator('..').getByRole('button', { name: 'Add', exact: true }).click();
    await page.getByRole('button', { name: /save changes/i }).first().click();

    // The Edit button coming back is what proves the save round-tripped.
    await expect(page.getByRole('button', { name: /^edit$/i }).first()).toBeVisible({ timeout: 15_000 });

    const res = await fetch(`${API_URL}/api/v1/workspaces/${wsId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(res.status).toBe(200);
    const attrs = (await res.json()).data.attributes;
    expect(attrs.engine).toBe('pulumi');
    expect(attrs.labels).toMatchObject({ team: 'stacks' });
  });

  test('the list badges it and the engine filter isolates it', async ({ page }) => {
    const token = getStoredToken();
    const stack = `${uniqueName('e2e-pulumi-f')}::dev`;
    const tfName = uniqueName('e2e-tf-f');
    await createPulumiWorkspace(token, stack);
    await createWorkspace(token, tfName);
    const label = stack.replace('::', ' / ');

    await page.goto('/workspaces');
    const pulumiRow = page.getByRole('link', { name: new RegExp(label) }).first();
    await expect(pulumiRow).toBeVisible({ timeout: 20_000 });
    await expect(pulumiRow.getByTestId('ws-engine-badge')).toHaveText('Pulumi');
    // Terraform rows carry no badge: a Terraform-only estate looks as it always did.
    await expect(page.getByRole('link', { name: tfName, exact: true }).getByTestId('ws-engine-badge')).toHaveCount(0);

    await page.getByTestId('ws-engine-filter').selectOption('pulumi');
    await expect(page.getByRole('link', { name: tfName, exact: true })).toHaveCount(0);
    await expect(pulumiRow).toBeVisible();
    await expect(page).toHaveURL(/engine=pulumi/);
  });

  test('its page shows Pulumi settings, and the bind-plan toggle round-trips', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createPulumiWorkspace(token, `${uniqueName('e2e-pulumi-b')}::dev`);

    await page.goto(`/workspaces/${wsId}`);
    await expect(page.getByTestId('ws-engine')).toHaveText('Pulumi', { timeout: 20_000 });
    // Terraform-only settings are hidden rather than shown and ignored.
    await expect(page.getByText('Execution Backend', { exact: true })).toHaveCount(0);
    await expect(page.getByText('Var Files', { exact: true })).toHaveCount(0);
    await expect(page.getByTestId('ws-bind-plan-value')).toHaveText('Disabled');

    await page.getByRole('button', { name: /^edit$/i }).first().click();
    await page.getByTestId('ws-bind-plan').check();
    await page.getByRole('button', { name: /save changes/i }).first().click();
    await expect(page.getByTestId('ws-bind-plan-value')).toHaveText('Enabled', { timeout: 15_000 });

    const res = await fetch(`${API_URL}/api/v1/workspaces/${wsId}`, { headers: { Authorization: `Bearer ${token}` } });
    expect((await res.json()).data.attributes['pulumi-bind-plan']).toBe(true);
  });

  test('a Terraform workspace page is unchanged', async ({ page }) => {
    const wsId = await createWorkspace(getStoredToken(), uniqueName('e2e-tf-page'));
    await page.goto(`/workspaces/${wsId}`);
    await expect(page.getByText('Execution Backend', { exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId('ws-engine')).toHaveCount(0);
    await expect(page.getByTestId('ws-bind-plan-value')).toHaveCount(0);
  });

  test('can be created from the UI', async ({ page }) => {
    const token = getStoredToken();
    const stack = `${uniqueName('e2e-pulumi-ui')}::dev`;

    await page.goto('/workspaces');
    await page.getByRole('button', { name: 'New Workspace' }).click();
    await page.locator('#ws-engine').selectOption('pulumi');
    await page.locator('#ws-name').fill(stack);
    await expect(page.getByTestId('ws-pulumi-name-hint')).toContainText('pulumi stack select');
    await expect(page.locator('#ws-backend')).toHaveCount(0);
    await page.getByRole('button', { name: 'Create Workspace' }).click();

    await expect(async () => {
      const res = await fetch(`${API_URL}/api/v1/workspaces/${stack}`, { headers: { Authorization: `Bearer ${token}` } });
      expect(res.status).toBe(200);
      expect((await res.json()).data.attributes.engine).toBe('pulumi');
    }).toPass({ timeout: 20_000 });
  });

  test('does not push the page sideways on a phone', async ({ page }) => {
    const wsId = await createPulumiWorkspace(getStoredToken(), `${uniqueName('e2e-pulumi-m')}::dev`);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/workspaces/${wsId}`);
    await expect(page.getByTestId('ws-engine')).toBeVisible({ timeout: 20_000 });
    await expectNoHorizontalPageScroll(page);
    await page.goto('/workspaces?engine=pulumi');
    await expect(page.getByTestId('ws-engine-filter')).toBeVisible({ timeout: 20_000 });
    await expectNoHorizontalPageScroll(page);
  });
});

/**
 * The surfaces a Pulumi workspace should not be offered, and the words it
 * should be described in (#1911).
 *
 * Three separate defects with one shape: a page built when every workspace was
 * Terraform. Security scanning was offered where the API accepts only `off`;
 * `-allow-empty-apply` was offered where nothing reads it; and the phase
 * vocabulary had been threaded into two places out of a dozen, so one screen
 * showed "Previewed" in the status card and "Planned" in the pill beside it.
 */
test.describe('Pulumi run vocabulary and Terraform-only surfaces', () => {
  test('security scanning is absent on Pulumi and present on Terraform', async ({ page }) => {
    const token = getStoredToken();
    const pulumiId = await createPulumiWorkspace(token, `${uniqueName('e2e-pulumi-sec')}::dev`);
    const tfId = await createWorkspace(token, uniqueName('e2e-tf-sec'));

    // Terraform: the block is there, and so are its three controls.
    await page.goto(`/workspaces/${tfId}`);
    await expect(page.getByRole('heading', { name: 'Security Scanning' })).toBeVisible({
      timeout: 20_000,
    });

    // Pulumi: gone entirely, not rendered-and-disabled. The API refuses any
    // enforcement but `off` there (#1567), so a control whose only accepted
    // value is the one already shown is a control that can only 422.
    await page.goto(`/workspaces/${pulumiId}`);
    await expect(page.getByTestId('ws-engine')).toHaveText('Pulumi', { timeout: 20_000 });
    await expect(page.getByRole('heading', { name: 'Security Scanning' })).toHaveCount(0);
    await expect(page.getByText('Block apply on findings')).toHaveCount(0);
  });

  test('the queue buttons and run options speak the engine', async ({ page }) => {
    const token = getStoredToken();
    const pulumiId = await createPulumiWorkspace(token, `${uniqueName('e2e-pulumi-q')}::dev`);
    const tfId = await createWorkspace(token, uniqueName('e2e-tf-q'));

    await page.goto(`/workspaces/${tfId}?tab=runs`);
    await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByRole('button', { name: 'Plan + apply', exact: true })).toBeVisible();

    await page.goto(`/workspaces/${pulumiId}?tab=runs`);
    await expect(page.getByRole('button', { name: 'Preview', exact: true })).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByRole('button', { name: 'Preview + update', exact: true })).toBeVisible();
    // The Terraform words are not merely additional — they are gone.
    await expect(page.getByRole('button', { name: 'Plan', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Plan + apply', exact: true })).toHaveCount(0);
  });

  test('allow-empty-apply is offered on Terraform and withheld on Pulumi', async ({ page }) => {
    const token = getStoredToken();
    const pulumiId = await createPulumiWorkspace(token, `${uniqueName('e2e-pulumi-opt')}::dev`);
    const tfId = await createWorkspace(token, uniqueName('e2e-tf-opt'));

    await page.goto(`/workspaces/${tfId}?tab=runs`);
    await page.getByRole('button', { name: 'Options', exact: true }).click();
    await expect(page.getByText('Allow Empty Apply')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('heading', { name: 'Plan Options' })).toBeVisible();
    // Terraform limits a run by resource address.
    await expect(page.getByPlaceholder('e.g. aws_instance.web, aws_s3_bucket.data')).toBeVisible();

    await page.goto(`/workspaces/${pulumiId}?tab=runs`);
    await page.getByRole('button', { name: 'Options', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Preview Options' })).toBeVisible({
      timeout: 15_000,
    });
    // `-allow-empty-apply` has no Pulumi equivalent — `PulumiRunOptions` has no
    // such field, so a ticked box would have been stored and silently dropped.
    await expect(page.getByText('Allow Empty Apply')).toHaveCount(0);
    // Target and replace DO reach Pulumi (as `--target`/`--replace`), so they
    // stay — addressed by URN, which is what the example has to show.
    await expect(page.getByText('Target resources')).toBeVisible();
    await expect(page.getByText('Replace resources')).toBeVisible();
    await expect(page.locator('input[placeholder^="e.g. urn:pulumi:"]')).toHaveCount(2);
    // Refresh-only and skip-refresh reach Pulumi too.
    await expect(page.getByText('Refresh Only')).toBeVisible();
    await expect(page.getByText('Skip Refresh')).toBeVisible();
  });

  test('the run page never shows two words for one run', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createPulumiWorkspace(token, `${uniqueName('e2e-pulumi-run')}::dev`);
    const runId = await seedPulumiRun(token, wsId);

    await page.goto(`/workspaces/${wsId}/runs/${runId}`);
    await expect(page.getByText(/Failed to load/i)).toHaveCount(0);

    // The tab strip is the loudest place the old wording survived.
    await expect(page.getByRole('button', { name: /^Preview\b/ })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('button', { name: /^Plan\b/ })).toHaveCount(0);

    // The speculative badge, and the Details field that mirrors it.
    //
    // `.first()` on every PRESENCE check: the page dual-renders for desktop and
    // phone from one source (AGENTS.md forbids forked trees), so each of these
    // matches twice and a bare `toBeVisible` is a strict-mode error rather than
    // a failure. The ABSENCE checks stay unscoped, which is the right way round
    // — one instance showing the new word proves the thread-through, and the old
    // word has to be gone from BOTH.
    await expect(page.getByText('preview only', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('plan only', { exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'Details', exact: true }).click();
    await expect(page.getByText('Preview Only', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Plan Only', { exact: true })).toHaveCount(0);

    // A missing `words` key renders as the raw key rather than throwing, so it
    // would otherwise pass type-check, the catalogue gate and a "no errors" test.
    await expect(page.getByText(/phases\.[a-z]+\.words\./)).toHaveCount(0);
  });

  test('a Terraform run page keeps every Terraform word', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-tf-run'));
    const runId = await seedRun(token, wsId);

    await page.goto(`/workspaces/${wsId}/runs/${runId}`);
    await expect(page.getByRole('button', { name: /^Plan\b/ })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText('plan only', { exact: true }).first()).toBeVisible();
    await expect(page.getByRole('button', { name: /^Preview\b/ })).toHaveCount(0);
  });

  test('neither surface pushes the page sideways on a phone', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createPulumiWorkspace(token, `${uniqueName('e2e-pulumi-rm')}::dev`);
    const runId = await seedPulumiRun(token, wsId);
    await page.setViewportSize({ width: 390, height: 844 });

    await page.goto(`/workspaces/${wsId}?tab=runs`);
    await expect(page.getByRole('button', { name: 'Preview', exact: true })).toBeVisible({
      timeout: 20_000,
    });
    await page.getByRole('button', { name: 'Options', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Preview Options' })).toBeVisible();
    await expectNoHorizontalPageScroll(page);

    await page.goto(`/workspaces/${wsId}/runs/${runId}`);
    // The run page's mobile view picker is a native <select>, not the tab bar.
    await expect(page.locator('#run-view-select')).toBeVisible({ timeout: 20_000 });
    await expect(page.locator('#run-view-select option', { hasText: 'Preview log' })).toHaveCount(1);
    await expectNoHorizontalPageScroll(page);
  });

  test('the cards above the runs tab speak the same engine as the tab', async ({ page }) => {
    // The whole point: these sit on the SAME page as the queue buttons, so a
    // Terraform word here is "two words for one run" one scroll apart. Positive
    // assertions on purpose — an absence check would also pass if the card
    // stopped rendering, which is exactly the false green to avoid here.
    const token = getStoredToken();
    const wsId = await createPulumiWorkspace(token, `${uniqueName('e2e-pulumi-cards')}::dev`);
    await lockWorkspace(token, wsId, 'e2e vocabulary check');

    await page.goto(`/workspaces/${wsId}`);
    await expect(page.getByTestId('ws-engine')).toHaveText('Pulumi', { timeout: 20_000 });

    // The padlock, immediately above the runs tab.
    await expect(page.getByText('No previews or updates can run.')).toBeVisible();
    await expect(page.getByText('No plans or applies can run.')).toHaveCount(0);

    // The cards below it.
    await expect(page.getByRole('heading', { name: 'Preview Expiry' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Plan Expiry' })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'AI Preview Summary' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'AI Plan Summary' })).toHaveCount(0);
    await expect(page.getByText('every preview-phase outcome', { exact: false })).toBeVisible();

    // Only the drift IGNORE RULES are Terraform's, and that control is absent —
    // the classifier reads Terraform attribute paths and reports a Pulumi
    // workspace CLEAN, so a rule here would fail OPEN. Drift detection ITSELF is
    // engine-neutral; its tooltip is checked separately, because a locked
    // workspace shows the lock reason there instead.
    await expect(page.getByText('Drift Ignore Rules')).toHaveCount(0);
  });

  test('a Terraform workspace keeps every one of those words', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-tf-cards'));
    await lockWorkspace(token, wsId, 'e2e vocabulary check');

    await page.goto(`/workspaces/${wsId}`);
    await expect(page.getByText('No plans or applies can run.')).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('heading', { name: 'Plan Expiry' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'AI Plan Summary' })).toBeVisible();
    // The Terraform-only control that a Pulumi workspace does not get.
    await expect(page.getByText('Drift Ignore Rules')).toBeVisible();
  });

  test('the drift check-now tooltip names the phase, on both engines', async ({ page }) => {
    // Its own test because the tooltip has three states and only one of them is
    // the phase word: a workspace with drift OFF reads "Enable drift detection
    // first" (the column defaults to false) and a LOCKED one reads "Workspace is
    // locked". Asserting it on a default workspace would have passed on a string
    // that never mentions a phase at all.
    const token = getStoredToken();
    const pulumiId = await createPulumiWorkspace(token, `${uniqueName('e2e-pulumi-drift')}::dev`);
    const tfId = await createWorkspace(token, uniqueName('e2e-tf-drift'));
    for (const id of [pulumiId, tfId]) {
      const res = await fetch(`${API_URL}/api/v1/workspaces/${id}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/vnd.api+json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          data: { type: 'workspaces', attributes: { 'drift-detection-enabled': true } },
        }),
      });
      expect(res.status).toBe(200);
    }

    await page.goto(`/workspaces/${pulumiId}`);
    await expect(page.getByTestId('ws-engine')).toHaveText('Pulumi', { timeout: 20_000 });
    await expect(page.getByTitle('Queue a preview-only run to check for drift')).toHaveCount(1);
    await expect(page.getByTitle('Queue a plan-only run to check for drift')).toHaveCount(0);

    await page.goto(`/workspaces/${tfId}`);
    await expect(page.getByTitle('Queue a plan-only run to check for drift')).toHaveCount(1, {
      timeout: 20_000,
    });
  });

  test('the create form re-words its auto-apply hint when the engine changes', async ({ page }) => {
    // The one surface where the engine is CHOSEN rather than already fixed, so
    // this is also the only place the binding itself can be tested: a
    // `phaseWord` wired to a constant instead of `newEngine` passes every unit
    // test and fails here. Drives the real selector rather than asserting a
    // string twice.
    await page.goto('/workspaces');
    await page.getByRole('button', { name: 'New Workspace' }).click();

    // Defaults to Terraform — and a single-engine deployment, which never
    // renders the selector at all, sees exactly this.
    await expect(page.getByText('apply every successful plan', { exact: false })).toBeVisible({
      timeout: 20_000,
    });

    await page.locator('#ws-engine').selectOption('pulumi');
    await expect(page.getByText('update on every successful preview', { exact: false })).toBeVisible();
    await expect(page.getByText('apply every successful plan', { exact: false })).toHaveCount(0);

    // The four MODE values are wire values, so they do not move with it.
    const modes = page.locator('#ws-auto-apply-mode option');
    await expect(modes).toHaveText(['never', 'always', 'create', 'create/update']);

    // ...and back, so the binding is proven in both directions rather than the
    // hint simply having been replaced wholesale.
    await page.locator('#ws-engine').selectOption('terraform');
    await expect(page.getByText('apply every successful plan', { exact: false })).toBeVisible();
  });

  test('the Versions tab names no CLI a Pulumi workspace cannot run', async ({ page }) => {
    // The one string where the engines need different SENTENCES rather than a
    // different word: no `pulumi` subcommand uploads a configuration version,
    // so naming one would send an operator looking for a command that does not
    // exist.
    const token = getStoredToken();
    const wsId = await createPulumiWorkspace(token, `${uniqueName('e2e-pulumi-ver')}::dev`);

    await page.goto(`/workspaces/${wsId}?tab=versions`);
    await expect(page.getByText('as soon as a VCS push triggers a run', { exact: false })).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByText('terraform plan', { exact: false })).toHaveCount(0);
  });
});
