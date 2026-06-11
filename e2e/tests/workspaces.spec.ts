import { test, expect } from '@playwright/test';

test.describe('Workspaces', () => {
  test('workspace list page loads', async ({ page }) => {
    await page.goto('/workspaces');

    // Page header should be visible
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();
  });

  test('create workspace and see it in list', async ({ page }) => {
    const wsName = `e2e-ws-${Date.now()}`;

    await page.goto('/workspaces');
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();

    // Open create form
    await page.click('button:has-text("New Workspace")');

    // Fill in workspace name
    await page.fill('input[placeholder*="workspace"]', wsName);

    // Submit
    await page.click('button:has-text("Create Workspace")');

    // Workspace should appear in the list
    await expect(page.locator(`text=${wsName}`)).toBeVisible({ timeout: 10_000 });
  });

  test('workspace detail shows tabs', async ({ page }) => {
    // Create a workspace first
    const wsName = `e2e-detail-${Date.now()}`;

    await page.goto('/workspaces');
    await expect(page.locator('h1:has-text("Workspaces")')).toBeVisible();

    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');

    // Click on the workspace to go to detail
    await page.click(`text=${wsName}`);

    // Verify tabs are present (use getByRole to avoid matching text in other elements)
    await expect(page.getByRole('button', { name: 'Overview' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Variables' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Runs' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'State' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Notifications' })).toBeVisible();
  });

  test('run-triggers tab uses a searchable workspace picker', async ({ page }) => {
    const src = `e2e-trg-src-${Date.now()}`;
    const dest = `e2e-trg-dest-${Date.now()}`;

    for (const name of [src, dest]) {
      await page.goto('/workspaces');
      await page.click('button:has-text("New Workspace")');
      await page.fill('input[placeholder*="workspace"]', name);
      await page.click('button:has-text("Create Workspace")');
      await expect(page.locator(`text=${name}`).first()).toBeVisible({ timeout: 10_000 });
    }

    // Open the destination workspace's Run Triggers tab
    await page.goto('/workspaces');
    await page.click(`text=${dest}`);
    await page.getByRole('button', { name: 'Run Triggers' }).click();

    // The picker is a search box + clickable list — NOT a free-text name input
    // (a typo can't 404 any more). Filter to the source, then click to add.
    const search = page.locator('input[placeholder*="Search workspaces to add"]');
    await expect(search).toBeVisible();
    await search.fill(src);

    const srcButton = page.getByRole('button', { name: src });
    await expect(srcButton).toBeVisible({ timeout: 10_000 });
    await srcButton.click();

    // The inbound edge appears live (id-based add → refetch), with a Remove control
    await expect(page.getByRole('button', { name: 'Remove' }).first()).toBeVisible({
      timeout: 10_000,
    });
  });

  test('workspace settings can be updated', async ({ page }) => {
    // Create a workspace
    const wsName = `e2e-settings-${Date.now()}`;

    await page.goto('/workspaces');
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');

    // Navigate to workspace detail
    await page.click(`text=${wsName}`);

    // Click Edit on the overview tab
    await page.click('button:has-text("Edit")');

    // Toggle auto-apply (it's a checkbox or toggle)
    const autoApplyToggle = page.locator('input[type="checkbox"]').first();
    const wasBefore = await autoApplyToggle.isChecked();
    await autoApplyToggle.click();

    // Save
    await page.click('button:has-text("Save")');

    // Wait for save to complete (Edit button re-appears)
    await expect(page.locator('button:has-text("Edit")')).toBeVisible({ timeout: 10_000 });

    // Reload and verify the toggle changed
    await page.reload();

    // Click Edit again to check the value
    await page.click('button:has-text("Edit")');
    const isAfter = await page.locator('input[type="checkbox"]').first().isChecked();
    expect(isAfter).not.toBe(wasBefore);
  });

  test('drift-ignore-rules editor adds, persists, and removes a rule', async ({ page }) => {
    // #482 — verify the workspace settings drift-ignore-rules editor
    // round-trips through the API. Failure cases the spec catches:
    //   - rule not added to the array on Enter / "Add" click
    //   - PATCH body missing the `drift-ignore-rules` attribute
    //   - re-rendered list doesn't reflect the saved value
    //   - Remove button doesn't drop the rule from state or PATCH
    const wsName = `e2e-drift-ignore-${Date.now()}`;
    const rule = 'module.eks*.argocd_cluster.*.config.tls_client_config.ca_data';

    await page.goto('/workspaces');
    await page.click('button:has-text("New Workspace")');
    await page.fill('input[placeholder*="workspace"]', wsName);
    await page.click('button:has-text("Create Workspace")');
    await page.click(`text=${wsName}`);

    // Enter edit mode and add a rule via the Add button (covers both
    // the keydown handler and the click handler — Add reads the same
    // state, so a working click implies the typed input was captured).
    await page.click('button:has-text("Edit")');
    const ruleInput = page.locator('input[placeholder*="argocd_cluster"]');
    await expect(ruleInput).toBeVisible();
    await ruleInput.fill(rule);
    // Two Add buttons exist (trigger-prefixes + drift-ignore-rules) —
    // scope to the one inside the drift-ignore row.
    const driftAddRow = page.locator('input[placeholder*="argocd_cluster"]').locator('..');
    await driftAddRow.locator('button:has-text("Add")').click();

    // The pill should render with the rule text.
    await expect(page.locator(`code:has-text("${rule}")`)).toBeVisible();

    // Save.
    await page.click('button:has-text("Save")');
    await expect(page.locator('button:has-text("Edit")')).toBeVisible({ timeout: 10_000 });

    // Reload and confirm the value persisted server-side (the
    // settings round-trip is the part that broke for trigger_prefixes
    // in v0.35.x — this guards us against the same bug landing here).
    await page.reload();
    await expect(page.locator(`code:has-text("${rule}")`)).toBeVisible();

    // Remove the rule via Edit → Remove → Save, and confirm it's
    // gone after reload.
    await page.click('button:has-text("Edit")');
    const removeBtn = page.locator(`code:has-text("${rule}")`).locator('..').locator('button:has-text("Remove")');
    await removeBtn.click();
    await page.click('button:has-text("Save")');
    await expect(page.locator('button:has-text("Edit")')).toBeVisible({ timeout: 10_000 });
    await page.reload();
    await expect(page.locator(`code:has-text("${rule}")`)).not.toBeVisible();
  });
});
