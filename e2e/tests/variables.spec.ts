import { test, expect, type Page, type Route } from '@playwright/test';
import { createWorkspace, getStoredToken, uniqueName } from '../helpers/api.js';

test.describe('Variables', () => {
  let workspaceId: string;
  const wsName = `e2e-vars-${Date.now()}`;

  test.beforeAll(async () => {
    // Create a workspace via API for variable tests.
    // Get admin token from the storageState file.
    const fs = await import('fs');
    const path = await import('path');
    const authPath = path.join(__dirname, '..', '.auth', 'admin.json');
    const authData = JSON.parse(fs.readFileSync(authPath, 'utf-8'));

    // Extract the session token from localStorage origins
    const origin = authData.origins?.find((o: { origin: string }) =>
      o.origin.includes('localhost'),
    );
    const authEntry = origin?.localStorage?.find(
      (e: { name: string }) => e.name === 'terrapod_auth',
    );
    const token = authEntry ? JSON.parse(authEntry.value).token : '';

    workspaceId = await createWorkspace(token, wsName);
  });

  test('create terraform variable', async ({ page }) => {
    await page.goto(`/workspaces/${workspaceId}?tab=variables`);

    // Click "Add Variable"
    await page.click('button:has-text("Add Variable")');

    // Fill in variable details
    await page.fill('#var-key', `TF_VAR_e2e_${Date.now()}`);
    await page.fill('#var-val', 'test-value');

    // Submit (the submit button also says "Add Variable")
    await page.click('form button:has-text("Add Variable")');

    // Variable should appear in the table. Scope to the <tr> — the value also
    // renders in the (hidden) mobile card, so a bare text= match is ambiguous.
    await expect(page.locator('tr:has-text("test-value")')).toBeVisible({ timeout: 10_000 });
  });

  test('create sensitive variable shows masked value', async ({ page }) => {
    const varKey = `SECRET_e2e_${Date.now()}`;

    await page.goto(`/workspaces/${workspaceId}?tab=variables`);

    await page.click('button:has-text("Add Variable")');
    await page.fill('#var-key', varKey);
    await page.fill('#var-val', 'super-secret');

    // Check the Sensitive checkbox
    const sensitiveCheckbox = page.locator('label:has-text("Sensitive") input[type="checkbox"]');
    await sensitiveCheckbox.check();

    await page.click('form button:has-text("Add Variable")');

    // Value should be masked
    const row = page.locator(`tr:has-text("${varKey}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.locator('text=***')).toBeVisible();
  });

  test('sensitive variable value is masked on entry, revealable', async ({ page }) => {
    await page.goto(`/workspaces/${workspaceId}?tab=variables`);

    await page.click('button:has-text("Add Variable")');
    const valField = page.locator('#var-val');
    await valField.fill('shoulder-surf-me');

    // Not sensitive yet -> value shown normally (no masking class).
    await expect(valField).not.toHaveClass(/text-masked/);

    // Mark sensitive -> the entry field masks (cross-browser disc font).
    await page.locator('label:has-text("Sensitive") input[type="checkbox"]').check();
    await expect(valField).toHaveClass(/text-masked/);

    // Show toggle reveals it; hiding masks again.
    await page.click('button[aria-label="Show value"]');
    await expect(valField).not.toHaveClass(/text-masked/);
    await page.click('button[aria-label="Hide value"]');
    await expect(valField).toHaveClass(/text-masked/);
  });

  test('git-auth category swaps the form to a credential builder and round-trips', async ({
    page,
  }) => {
    // #1028 / #1042: selecting a git category must ADAPT the form (the user's
    // complaint was that the generic value field stayed put), expose a
    // credential-source picker, and store a masked, URL-pattern-scoped var.
    const pattern = `github.com/e2e-${Date.now()}`;
    await page.goto(`/workspaces/${workspaceId}?tab=variables`);
    await page.click('button:has-text("Add Variable")');

    // Baseline: the generic value field is present for a normal category.
    await expect(page.locator('#var-val')).toBeVisible();

    // Switch to the git HTTPS credential category.
    await page.selectOption('#var-cat', 'git_http_auth');

    // The form ADAPTED: the generic value field is gone; the git builder is shown.
    await expect(page.locator('#var-val')).toHaveCount(0);
    await expect(page.locator('#git-source')).toBeVisible();

    // The key field is now the URL pattern; use a static token source so the
    // test needs no configured VCS connection.
    await page.fill('#var-key', pattern);
    await page.selectOption('#git-source', 'static');
    await page.fill('#git-token', 'ghp_e2e_fake_token');

    await page.click('form button:has-text("Add Variable")');

    // Row renders with the VISIBLE URL pattern (not secret) and a MASKED value
    // (the token is forced sensitive; it must never render in the table).
    const row = page.locator(`tr:has-text("${pattern}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row.locator('text=***')).toBeVisible();
    await expect(page.locator(`text=ghp_e2e_fake_token`)).toHaveCount(0);
  });

  test('delete variable removes it from list', async ({ page }) => {
    const varKey = `DELETE_e2e_${Date.now()}`;

    await page.goto(`/workspaces/${workspaceId}?tab=variables`);

    // Create a variable to delete
    await page.click('button:has-text("Add Variable")');
    await page.fill('#var-key', varKey);
    await page.fill('#var-val', 'to-be-deleted');
    await page.click('form button:has-text("Add Variable")');

    // Wait for it to appear
    const row = page.locator(`tr:has-text("${varKey}")`);
    await expect(row).toBeVisible({ timeout: 10_000 });

    // Delete it — a native confirm() now guards the delete (#719); accept it.
    page.once('dialog', (d) => d.accept());
    await row.locator('button:has-text("Delete")').click();

    // Should be gone
    await expect(row).not.toBeVisible({ timeout: 10_000 });
  });
});

test.describe('Vault value source (#1439)', () => {
  const API_URL = process.env.API_URL || 'http://localhost:8000'

  /** Pretend the deployment has Vault configured. The e2e stack deliberately
   *  does not, which is what makes the "not offered" test below real. */
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

  test('the source picker is not offered when Vault is not configured', async ({ page }) => {
    // Unstubbed: the stack really has no Vault, so this asserts the gate rather
    // than a mock of it. Offering a source that cannot work would produce a
    // variable that fails its first run.
    const token = getStoredToken()
    // Agent mode deliberately: the picker is also hidden under local
    // execution, so a local workspace would pass this for the wrong reason
    // and stop testing the Vault-not-configured gate at all.
    const wsId = await createWorkspace(token, uniqueName('e2enovault'), {
      'execution-mode': 'agent',
    })

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await expect(page.locator('#var-key')).toBeVisible()
    await expect(page.locator('#var-source')).toHaveCount(0)
  })

  test('the source picker is not offered on a local workspace, even with Vault configured', async ({ page }) => {
    // The other direction of the same gate: Vault is available here, but a
    // reference is resolved on the listener claim path, so under local
    // execution it would deliver nothing and the API refuses to store it.
    // Offering the source would mean filling in the builder to meet a 422.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2evaultlocal'), {
      'execution-mode': 'local',
    })
    await withVault(page)

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await expect(page.locator('#var-key')).toBeVisible()
    await expect(page.locator('#var-source')).toHaveCount(0)
  })

  test('choosing Vault swaps the value box for the reference builder', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2evaultform'), {
      'execution-mode': 'agent',
    })
    await withVault(page)

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await expect(page.locator('#var-val')).toBeVisible()

    await page.locator('#var-source').selectOption('vault')

    // The value box gives way to coordinates — you cannot type a literal into a
    // variable whose value lives in Vault.
    await expect(page.locator('#var-val')).toHaveCount(0)
    await expect(page.locator('#add-mount')).toBeVisible()
    await expect(page.locator('#add-path')).toBeVisible()
    await expect(page.locator('#add-field')).toBeVisible()
    // Always shown, even with one instance: which Vault a credential comes from
    // is the thing worth being explicit about.
    await expect(page.locator('#add-vault')).toBeVisible()
  })

  test('a reference is built, saved, and shown as coordinates not asterisks', async ({ page }) => {
    const token = getStoredToken()
    // Vault references only resolve under agent execution, so the API
    // refuses to store one on a local workspace (_reject_vault_on_local).
    const wsId = await createWorkspace(token, uniqueName('e2evaultsave'), {
      'execution-mode': 'agent',
    })
    await withVault(page)

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-key').fill('NETBOX_TOKEN')
    await page.locator('#var-cat').selectOption('env')
    await page.locator('#var-source').selectOption('vault')
    await page.locator('#add-mount').fill('secret')
    await page.locator('#add-path').fill('apps/netbox')
    await page.locator('#add-field').fill('apitoken')
    await page.getByRole('button', { name: 'Add Variable', exact: true }).last().click()

    const row = page.locator('tr').filter({ hasText: 'NETBOX_TOKEN' })
    await expect(row).toBeVisible({ timeout: 10_000 })
    // The stored value is a path, not a secret — masking it would hide
    // configuration while concealing nothing.
    await expect(row.getByText('secret/apps/netbox')).toBeVisible()
    await expect(row.getByText('***')).toHaveCount(0)
  })

  test('a saved reference can be edited — the fields come back populated', async ({ page }) => {
    // The defect this guards: editing was value-only, so a vault-backed
    // variable could be created and then never corrected.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2evaultedit'), {
      'execution-mode': 'agent',
    })
    await withVault(page)

    const res = await fetch(`${API_URL}/api/v2/workspaces/${wsId}/vars`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: {
          type: 'vars',
          attributes: {
            key: 'EDIT_ME',
            category: 'env',
            'value-source': 'vault',
            value: JSON.stringify({
              source: 'vault', mount: 'secret', path: 'apps/original', field: 'token',
            }),
          },
        },
      }),
    })
    expect(res.status).toBe(201)

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    const row = page.locator('tr').filter({ hasText: 'EDIT_ME' })
    await expect(row).toBeVisible({ timeout: 10_000 })
    await row.getByRole('button', { name: 'Edit' }).click()

    // Populated from the stored reference, not blanked the way a sensitive
    // value is — otherwise every edit would rebuild it from nothing.
    const mount = page.locator('[id$="-mount"]').first()
    await expect(mount).toHaveValue('secret')
    await expect(page.locator('[id$="-path"]').first()).toHaveValue('apps/original')

    await page.locator('[id$="-path"]').first().fill('apps/changed')
    await page.getByRole('button', { name: 'Save' }).click()

    await expect(page.locator('tr').filter({ hasText: 'EDIT_ME' })
      .getByText('secret/apps/changed')).toBeVisible({ timeout: 10_000 })
  })
})
