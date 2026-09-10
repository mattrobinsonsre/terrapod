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
import { API_URL, getStoredToken, createPulumiWorkspace, createWorkspace, uniqueName } from '../helpers/api';
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
