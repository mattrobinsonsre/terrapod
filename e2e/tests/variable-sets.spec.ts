import { test, expect, type Page, type Route } from '@playwright/test';
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api';

const API_URL = process.env.API_URL || 'http://localhost:8000';

test.describe('Variable Sets', () => {
  test('variable set list page loads', async ({ page }) => {
    await page.goto('/admin/variable-sets');
    await expect(page.locator('h1:has-text("Variable Sets")')).toBeVisible();
  });

  test('create global variable set appears with badge', async ({ page }) => {
    const name = `e2evs${Date.now()}`;

    await page.goto('/admin/variable-sets');
    await expect(page.locator('h1:has-text("Variable Sets")')).toBeVisible();

    // Toggle create form open
    await page.click('button:has-text("New Variable Set")');
    await page.fill('#vs-name', name);
    await page.fill('#vs-desc', 'E2E test set');

    // Check the Global checkbox
    const globalCheckbox = page.locator('label:has-text("Global") input[type="checkbox"]');
    await globalCheckbox.check();

    // Submit button says "Create Variable Set"
    await page.click('button[type="submit"]:has-text("Create Variable Set")');

    // Variable set should appear in the table with Global badge
    const row = page.locator(`tr:has-text("${name}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.locator('text=Global').filter({ visible: true })).toBeVisible();
  });

  test('navigate to detail page shows tabs', async ({ page }) => {
    const name = `e2evsdet${Date.now()}`;

    await page.goto('/admin/variable-sets');

    // Create a variable set
    await page.click('button:has-text("New Variable Set")');
    await page.fill('#vs-name', name);
    await page.fill('#vs-desc', 'Detail test');
    await page.click('button[type="submit"]:has-text("Create Variable Set")');

    // Click through to detail via the link in the row
    await page.click(`a:has-text("${name}")`);

    // Tabs should be visible
    await expect(page.getByRole('button', { name: 'Settings' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Variables' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Workspaces' })).toBeVisible();
  });

  test('add variable on detail page', async ({ page }) => {
    const name = `e2evsvar${Date.now()}`;
    const varKey = `E2E_VAR_${Date.now()}`;

    await page.goto('/admin/variable-sets');

    // Create variable set
    await page.click('button:has-text("New Variable Set")');
    await page.fill('#vs-name', name);
    await page.click('button[type="submit"]:has-text("Create Variable Set")');

    // Navigate to detail
    await page.click(`a:has-text("${name}")`);
    await expect(page.getByRole('button', { name: 'Variables' })).toBeVisible({ timeout: 10_000 });

    // Switch to Variables tab
    await page.getByRole('button', { name: 'Variables' }).click();

    // Add a variable
    await page.click('button:has-text("Add Variable")');
    await page.fill('#var-key', varKey);
    await page.fill('#var-val', 'test-value');
    await page.click('form button:has-text("Add Variable")');

    // Variable should appear in the table
    await expect(page.locator(`text=${varKey}`)).toBeVisible({ timeout: 10_000 });
  });

  test('delete variable set from detail page', async ({ page }) => {
    const name = `e2evsdel${Date.now()}`;

    await page.goto('/admin/variable-sets');

    // Create variable set to delete
    await page.click('button:has-text("New Variable Set")');
    await page.fill('#vs-name', name);
    await page.click('button[type="submit"]:has-text("Create Variable Set")');

    // Navigate to detail page
    await page.click(`a:has-text("${name}")`);
    await expect(page.getByRole('button', { name: 'Settings' })).toBeVisible({ timeout: 10_000 });

    // Settings tab should already be active; find Delete section
    await page.click('button:has-text("Delete")');

    // Click "Confirm Delete"
    await page.click('button:has-text("Confirm Delete")');

    // Should redirect back to list page and the varset should be gone
    await expect(page.locator('h1:has-text("Variable Sets")')).toBeVisible({ timeout: 10_000 });
    await expect(page.locator(`text=${name}`)).not.toBeVisible({ timeout: 5_000 });
  });
});

test.describe('Variable set assignment rules (#1440)', () => {

  async function createRuleVarset(token: string, name: string, rule: Record<string, unknown>) {
    const res = await fetch(`${API_URL}/api/v2/organizations/default/varsets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ data: { attributes: { name, 'assignment-rule': rule } } }),
    })
    expect(res.status).toBe(201)
    return (await res.json()).data.id as string
  }

  test('a workspace shows the sets that reach it, and why', async ({ page }) => {
    // The gap this closes: the association is managed from the variable set, so
    // from the workspace there was previously no way to see which sets apply —
    // and with a rule there may be no explicit binding to look up at all.
    const token = getStoredToken()
    const wsName = uniqueName('e2erulews')
    const wsId = await createWorkspace(token, wsName, { labels: { e2erule: wsName } })
    const vsName = uniqueName('e2erulevs')
    await createRuleVarset(token, vsName, { labels: { e2erule: wsName } })

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    const entry = page.locator('li').filter({ hasText: vsName })
    await expect(entry).toBeVisible({ timeout: 10_000 })
    // The source is the point: "someone bound this" and "it matches a rule"
    // call for completely different actions.
    await expect(entry.getByText('Matched by rule')).toBeVisible()
  })

  test('the set lists the workspaces its rule reaches, without offering a remove', async ({ page }) => {
    // A rule-matched workspace has no binding row to delete, so a Remove button
    // there would silently do nothing.
    const token = getStoredToken()
    const wsName = uniqueName('e2eruleblast')
    await createWorkspace(token, wsName, { labels: { e2eblast: wsName } })
    const vsName = uniqueName('e2eblastvs')
    const vsId = await createRuleVarset(token, vsName, { labels: { e2eblast: wsName } })

    await page.goto(`/admin/variable-sets/${vsId}?tab=workspaces`)
    const row = page.locator('tr').filter({ hasText: wsName })
    await expect(row).toBeVisible({ timeout: 10_000 })
    // Exact match: "Rule" is a substring of "Managed by rule" in the same row,
    // so a loose text match resolves to several elements and asserts nothing in
    // particular.
    await expect(row.getByText('Rule', { exact: true })).toBeVisible()
    await expect(row.getByText('Managed by rule', { exact: true })).toBeVisible()
    // The point of the row: no unbind control, because there is no explicit
    // binding to remove.
    await expect(row.getByRole('button', { name: 'Remove' })).toHaveCount(0)
  })

  test('an unparseable rule is refused at write time', async () => {
    // Failing closed matters here: a rule that does not parse matches nothing,
    // so accepting it would leave a set that silently applies to no workspace.
    const token = getStoredToken()
    const res = await fetch(`${API_URL}/api/v2/organizations/default/varsets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: { attributes: { name: uniqueName('e2ebad'), 'assignment-rule': { nope: 1 } } },
      }),
    })
    expect(res.status).toBe(422)
  })
})

test.describe('Assignment rule editor (regression for the inert-save bug)', () => {
  test('a rule created through the editor is actually saved', async ({ page }) => {
    // Every other #1440 spec creates rules via the API and asserts on rendering.
    // That is why a PATCH body missing `assignment-rule` shipped: the editor
    // rendered, previewed a live match count, reported success, and saved
    // nothing. This drives the form.
    const token = getStoredToken()
    const wsName = uniqueName('e2eeditorws')
    await createWorkspace(token, wsName, { labels: { e2eeditor: wsName } })

    const vsName = uniqueName('e2eeditorvs')
    const res = await fetch(`${API_URL}/api/v2/organizations/default/varsets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ data: { attributes: { name: vsName } } }),
    })
    expect(res.status).toBe(201)
    const vsId = (await res.json()).data.id

    await page.goto(`/admin/variable-sets/${vsId}?tab=settings`)
    await page.getByRole('button', { name: 'Edit' }).click()
    await page.getByRole('checkbox', { name: /assignment rule/i }).check()

    // Add the label through the editor's own controls.
    await page.getByPlaceholder('key').last().fill('e2eeditor')
    await page.getByPlaceholder('value').last().fill(wsName)
    await page.getByRole('button', { name: 'Add' }).last().click()

    await page.getByRole('button', { name: 'Save' }).click()

    // The assertion that matters: re-read from the server, not the optimistic
    // client state the old code was quietly showing.
    await expect(async () => {
      const check = await fetch(`${API_URL}/api/v2/varsets/${vsId}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      const rule = (await check.json()).data.attributes['assignment-rule']
      expect(rule).not.toBeNull()
      expect(rule.labels.e2eeditor).toBe(wsName)
    }).toPass({ timeout: 10_000 })
  })

})

test.describe('Variable set Vault source (#1439)', () => {
  /** Pretend the deployment has Vault configured. The e2e stack deliberately
   *  does not — TERRAPOD_VAULT__ENABLED is unset and the config default is
   *  False — which is what makes the "not offered" test below real, and what
   *  made an unstubbed version of this suite fail deterministically. */
  async function withVault(page: Page, instances = ['default'], defaultInstance = 'default') {
    await page.route('**/api/terrapod/v1/vault/availability', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: {
            type: 'vault-availability',
            id: 'vault',
            attributes: {
              enabled: true,
              instances,
              'default-instance': defaultInstance,
            },
          },
        }),
      }),
    )
  }

  async function makeVarset(page: Page): Promise<string> {
    const token = getStoredToken()
    const res = await page.request.post(`${API_URL}/api/v2/organizations/default/varsets`, {
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      data: { data: { attributes: { name: uniqueName('e2e-vault-vs') } } },
    })
    expect(res.status()).toBe(201)
    return (await res.json()).data.id
  }

  test('the source picker is not offered when Vault is not configured', async ({ page }) => {
    // Unstubbed: the stack really has no Vault, so this asserts the gate rather
    // than a mock of it. Offering a source that cannot resolve would let an
    // operator save a reference that silently produces nothing at run time.
    const vsId = await makeVarset(page)
    await page.goto(`/admin/variable-sets/${vsId}?tab=variables`)
    await page.click('button:has-text("Add Variable")')

    await expect(page.locator('#var-val')).toBeVisible()
    await expect(page.locator('#var-source')).toHaveCount(0)
  })

  test('a variable set offers the Vault source when Vault is enabled', async ({ page }) => {
    // The "define once, apply to many" path: a Vault-backed credential set on a
    // variable SET, reachable from the UI (not only via the API/SDK).
    const vsId = await makeVarset(page)
    await withVault(page)

    await page.goto(`/admin/variable-sets/${vsId}?tab=variables`)
    await page.click('button:has-text("Add Variable")')

    // Choosing the source swaps the value box for the reference builder.
    await page.locator('#var-source').selectOption('vault')
    await expect(page.locator('#add-mount')).toBeVisible()
    await expect(page.locator('#var-val')).toHaveCount(0)
  })
})
