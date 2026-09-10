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
import { API_URL, getStoredToken, createPulumiWorkspace, uniqueName } from '../helpers/api';

test.describe('Pulumi workspace', () => {
  test('appears in the list, opens, and saves an edit', async ({ page }) => {
    const token = getStoredToken();
    const name = `${uniqueName('e2e-pulumi')}::dev`;
    const wsId = await createPulumiWorkspace(token, name);

    await page.goto('/workspaces');
    await expect(page.getByText(name, { exact: true }).first()).toBeVisible({ timeout: 20_000 });

    await page.goto(`/workspaces/${wsId}`);
    await expect(page.getByText(name, { exact: true }).first()).toBeVisible({ timeout: 20_000 });

    // Edit a setting every engine has, and save it.
    await page.getByRole('button', { name: /^edit$/i }).first().click();
    await page.getByPlaceholder('key', { exact: true }).fill('team');
    await page.getByPlaceholder('value', { exact: true }).fill('stacks');
    await page.getByRole('button', { name: 'Add', exact: true }).click();
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
});
