import { test, expect } from '@playwright/test';

test.describe('Roles & Assignments', () => {
  test('roles tab shows built-in roles', async ({ page }) => {
    await page.goto('/admin/roles');
    await expect(page.locator('h1:has-text("Roles & Assignments")')).toBeVisible();

    // Built-in role headings should be visible (use h3 to avoid matching badges)
    await expect(page.locator('h3:has-text("admin")')).toBeVisible();
    await expect(page.locator('h3:has-text("audit")')).toBeVisible();
    await expect(page.locator('h3:has-text("everyone")')).toBeVisible();

    // At least one "built-in" badge should be present
    await expect(page.locator('text=built-in').first()).toBeVisible();
  });

  test('create custom role appears in list', async ({ page }) => {
    const roleName = `e2erole${Date.now()}`;

    await page.goto('/admin/roles');
    await expect(page.locator('h1:has-text("Roles & Assignments")')).toBeVisible();

    // Toggle create form open
    await page.click('button:has-text("Create Role")');

    await page.fill('#r-name', roleName);
    await page.selectOption('#r-perm', 'write');
    // Registry permission is its own field, independent of workspace (#registry-permission).
    await page.selectOption('#r-registry-perm', 'write');
    await page.fill('#r-desc', 'E2E test role');

    // Submit button says "Create Role"
    await page.click('button[type="submit"]:has-text("Create Role")');

    // Role should appear in the list, carrying the registry badge.
    const card = page.locator(`div:has(h3:has-text("${roleName}"))`).first();
    await expect(card.locator(`h3:has-text("${roleName}")`)).toBeVisible({ timeout: 10_000 });
    await expect(card.locator('text=registry: write')).toBeVisible();
  });

  test('create assignment on assignments tab', async ({ page }) => {
    await page.goto('/admin/roles');

    // Switch to Assignments tab
    await page.click('button:has-text("Assignments")');

    // Toggle assignment form open
    await page.click('button:has-text("Add Assignment")');

    // Fill assignment form
    await page.selectOption('#a-provider', 'local');
    await page.fill('#a-email', 'e2e-user@terrapod.local');

    // Check the "audit" role checkbox
    const auditCheckbox = page.locator('label:has-text("audit") input[type="checkbox"]');
    await auditCheckbox.check();

    // Submit button says "Set Roles"
    await page.click('button[type="submit"]:has-text("Set Roles")');

    // Assignment should appear in the table
    await expect(
      page.locator('tr:has-text("e2e-user@terrapod.local")'),
    ).toBeVisible({ timeout: 10_000 });
  });

  test('author a role by granular capabilities (#585)', async ({ page }) => {
    // Capability authoring: pick a granular set that is NOT a preset expansion
    // (run:read + run:plan but NOT run:apply), so the server derives the level
    // as the literal "custom" and the row renders the purple ws badge.
    const roleName = `e2ecap${Date.now()}`;

    await page.goto('/admin/roles');
    await expect(page.locator('h1:has-text("Roles & Assignments")')).toBeVisible();

    // Open the create form.
    await page.click('button:has-text("Create Role")');
    await page.fill('#r-name', roleName);
    await page.fill('#r-desc', 'E2E capability-authored role');

    // Open the "Advanced — capabilities" matrix.
    await page.click('button:has-text("Advanced — capabilities")');

    // The workspace "read" preset pre-checks run:read (among others). Add
    // run:plan explicitly but leave run:apply unchecked — this is not any single
    // preset expansion, so the selection becomes Custom.
    const runRead = page.locator('#create-cap-run-read');
    const runPlan = page.locator('#create-cap-run-plan');
    const runApply = page.locator('#create-cap-run-apply');
    await expect(runRead).toBeChecked();
    await runPlan.check();
    await expect(runPlan).toBeChecked();
    await expect(runApply).not.toBeChecked();

    // The matrix header now shows the Custom badge (selection diverged from presets).
    await expect(
      page.locator('span:has-text("Custom")').first(),
    ).toBeVisible();

    // Submit.
    await page.click('button[type="submit"]:has-text("Create Role")');

    // The role appears in the list. Scope every assertion to THIS role's card.
    const card = page.locator('div.rounded-lg').filter({ has: page.locator(`h3:has-text("${roleName}")`) }).first();
    await expect(card.locator(`h3:has-text("${roleName}")`)).toBeVisible({ timeout: 10_000 });

    // Derived level renders as the purple "custom" workspace badge.
    await expect(card.locator('span:has-text("ws: custom")')).toBeVisible();

    // Expand the capability chip list and confirm run:plan is granted but
    // run:apply is not.
    await card.locator('button:has-text("capabilities")').click();
    // Chips are exact-text spans; use getByText(exact) so run:apply doesn't
    // match run:apply-destroy and run:plan doesn't match a broader token.
    await expect(card.getByText('run:plan', { exact: true })).toBeVisible();
    await expect(card.getByText('run:apply', { exact: true })).toHaveCount(0);

    // Teardown: delete the role (other specs in this file self-teardown).
    page.once("dialog", (d) => d.accept());
    await card.locator('button:has-text("Delete")').click();
    await expect(page.locator(`h3:has-text("${roleName}")`)).not.toBeVisible({ timeout: 10_000 });
  });

  test('delete custom role removes it from list', async ({ page }) => {
    const roleName = `e2edel${Date.now()}`;

    await page.goto('/admin/roles');

    // Create the role first
    await page.click('button:has-text("Create Role")');
    await page.fill('#r-name', roleName);
    await page.selectOption('#r-perm', 'read');
    await page.fill('#r-desc', 'To be deleted');
    await page.click('button[type="submit"]:has-text("Create Role")');

    await expect(page.locator(`h3:has-text("${roleName}")`)).toBeVisible({ timeout: 10_000 });

    // Find the role card (a rounded-lg div containing the h3 with the role name)
    const card = page.locator('div.rounded-lg').filter({ has: page.locator(`h3:has-text("${roleName}")`) });
    page.once("dialog", (d) => d.accept());

    await card.locator('button:has-text("Delete")').click();

    // Role should be gone
    await expect(page.locator(`h3:has-text("${roleName}")`)).not.toBeVisible({ timeout: 10_000 });
  });
});
