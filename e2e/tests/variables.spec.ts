import path from 'path';
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

  // ── File delivery (#1619) ─────────────────────────────────────────

  /** The stored reference, read back through the API rather than the UI —
   *  the point is what was saved, not what the page chose to show. */
  async function readRef(token: string, wsId: string, key: string): Promise<Record<string, unknown>> {
    const res = await fetch(`${API_URL}/api/v2/workspaces/${wsId}/vars`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(res.status).toBe(200)
    const v = (await res.json()).data.find(
      (d: { attributes: { key: string } }) => d.attributes.key === key,
    )
    expect(v, `variable ${key} exists`).toBeTruthy()
    return JSON.parse(v.attributes.value)
  }

  test('file delivery is built, saved, and survives a reload (#1619)', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2evaultfile'), {
      'execution-mode': 'agent',
    })
    await withVault(page)

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-key').fill('GCP_CREDS')
    await page.locator('#var-cat').selectOption('env')
    await page.locator('#var-source').selectOption('vault')
    await page.locator('#add-mount').fill('secret')
    await page.locator('#add-path').fill('apps/gcp')
    await page.locator('#add-field').fill('sa_json')
    // The name box only exists once file delivery is on.
    await expect(page.locator('#add-file-name')).toHaveCount(0)
    await page.locator('#add-file').check()
    await page.locator('#add-file-name').fill('gcp/adc.json')
    await page.getByRole('button', { name: 'Add Variable', exact: true }).last().click()

    const row = () => page.locator('tr').filter({ hasText: 'GCP_CREDS' })
    await expect(row().getByText('gcp/adc.json')).toBeVisible({ timeout: 10_000 })
    await page.reload()
    await expect(row().getByText('gcp/adc.json')).toBeVisible({ timeout: 10_000 })
    expect((await readRef(token, wsId, 'GCP_CREDS')).file).toEqual({ name: 'gcp/adc.json' })
  })

  test('an edit keeps file.name, method and data; turning file delivery off removes only file (#1619)', async ({ page }) => {
    // The defect this guards: the UI rebuilt a reference from the fields it
    // renders, so editing anything silently dropped `method` and `data` —
    // and would have dropped `file` with them.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2evaultkeep'), {
      'execution-mode': 'agent',
    })
    await withVault(page)

    const seeded = {
      source: 'vault', engine: 'dynamic', method: 'POST',
      mount: 'pki', path: 'issue/example', field: 'certificate',
      data: { common_name: 'app.example.internal', ttl: '1h' },
      file: { name: 'tls/cert.pem' },
    }
    const res = await fetch(`${API_URL}/api/v2/workspaces/${wsId}/vars`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: {
          type: 'vars',
          attributes: {
            key: 'TLS_CERT', category: 'env', 'value-source': 'vault',
            value: JSON.stringify(seeded),
          },
        },
      }),
    })
    expect(res.status).toBe(201)

    const openEditor = async () => {
      await page.goto(`/workspaces/${wsId}?tab=variables`)
      const row = page.locator('tr').filter({ hasText: 'TLS_CERT' })
      await expect(row).toBeVisible({ timeout: 10_000 })
      await row.getByRole('button', { name: 'Edit' }).click()
    }

    // 1. Edit an unrelated field. The editor comes back with file delivery on.
    await openEditor()
    await expect(page.locator('[id$="-file"]:visible').first()).toBeChecked()
    await expect(page.locator('[id$="-file-name"]:visible').first()).toHaveValue('tls/cert.pem')
    await page.locator('[id$="-field"]:visible').first().fill('private_key')
    await page.getByRole('button', { name: 'Save' }).click()

    await page.reload()
    await expect.poll(async () => (await readRef(token, wsId, 'TLS_CERT')).field).toBe('private_key')
    const edited = await readRef(token, wsId, 'TLS_CERT')
    expect(edited.method).toBe('POST')
    expect(edited.data).toEqual(seeded.data)
    expect(edited.engine).toBe('dynamic')
    expect(edited.file).toEqual({ name: 'tls/cert.pem' })

    // 2. Turn file delivery off: file goes, nothing else does.
    await openEditor()
    await page.locator('[id$="-file"]:visible').first().uncheck()
    await expect(page.locator('[id$="-file-name"]:visible')).toHaveCount(0)
    await page.getByRole('button', { name: 'Save' }).click()

    await page.reload()
    await expect.poll(async () => 'file' in (await readRef(token, wsId, 'TLS_CERT'))).toBe(false)
    const off = await readRef(token, wsId, 'TLS_CERT')
    expect(off.method).toBe('POST')
    expect(off.data).toEqual(seeded.data)
    expect(off.field).toBe('private_key')
    await expect(page.locator('tr').filter({ hasText: 'TLS_CERT' }).getByText('tls/cert.pem')).toHaveCount(0)
  })

  // ── Templates, formats and encoding (#1648) ───────────────────────

  test('a templated file is built in the form, saved without a field, and survives a reload (#1648)', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2evaulttpl'), {
      'execution-mode': 'agent',
    })
    await withVault(page)
    const template =
      '[default]\naws_access_key_id = {{ access_key }}\naws_secret_access_key = {{ secret_key }}\n'

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-key').fill('AWS_SHARED_CREDENTIALS_FILE')
    await page.locator('#var-cat').selectOption('env')
    await page.locator('#var-source').selectOption('vault')
    await page.locator('#add-engine').selectOption('dynamic')
    await page.locator('#add-mount').fill('aws')
    await page.locator('#add-path').fill('creds/deploy')
    await page.locator('#add-file').check()
    await page.locator('#add-file-name').fill('~/.aws/credentials')
    // One field is the default kind; a template reads the whole secret, so
    // the field box goes away when it is chosen.
    await expect(page.locator('#add-file-content')).toHaveValue('field')
    await expect(page.locator('#add-field')).toBeVisible()
    await page.locator('#add-file-content').selectOption('template')
    await expect(page.locator('#add-field')).toHaveCount(0)
    await page.locator('#add-file-template').fill(template)
    await page.getByRole('button', { name: 'Add Variable', exact: true }).last().click()

    const row = () => page.locator('tr').filter({ hasText: 'AWS_SHARED_CREDENTIALS_FILE' })
    await expect(row().getByText('~/.aws/credentials')).toBeVisible({ timeout: 10_000 })
    await page.reload()
    await expect(row().getByText('Template', { exact: true })).toBeVisible({ timeout: 10_000 })

    const saved = await readRef(token, wsId, 'AWS_SHARED_CREDENTIALS_FILE')
    expect('field' in saved).toBe(false)
    expect(saved.engine).toBe('dynamic')
    expect(saved.file).toEqual({ name: '~/.aws/credentials', template })

    // The editor comes back on the template, with the text exactly as saved.
    await row().getByRole('button', { name: 'Edit' }).click()
    await expect(page.locator('[id$="-file-content"]:visible').first()).toHaveValue('template')
    await expect(page.locator('[id$="-file-template"]:visible').first()).toHaveValue(template)
    await expect(page.locator('[id$="-field"]:visible')).toHaveCount(0)
  })

  test('a whole-secret format with fields round-trips, and an edit keeps what it did not touch (#1648)', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2evaultfmt'), {
      'execution-mode': 'agent',
    })
    await withVault(page)

    const seeded = {
      source: 'vault', mount: 'secret', path: 'apps/db',
      file: { name: 'db.env', format: 'env', fields: ['DB_USER', 'DB_PASS'] },
    }
    const res = await fetch(`${API_URL}/api/v2/workspaces/${wsId}/vars`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: {
          type: 'vars',
          attributes: {
            key: 'DB_ENV_FILE', category: 'env', 'value-source': 'vault',
            value: JSON.stringify(seeded),
          },
        },
      }),
    })
    expect(res.status).toBe(201)

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    const row = page.locator('tr').filter({ hasText: 'DB_ENV_FILE' })
    // 'env' is both the variable's category and the file's format here, so
    // assert on the file name, which the row shows exactly once.
    await expect(row.getByText('db.env', { exact: true })).toBeVisible({ timeout: 10_000 })
    await row.getByRole('button', { name: 'Edit' }).click()
    await expect(page.locator('[id$="-file-content"]:visible').first()).toHaveValue('format')
    await expect(page.locator('[id$="-file-format"]:visible').first()).toHaveValue('env')
    await expect(page.locator('[id$="-file-fields"]:visible').first()).toHaveValue('DB_USER, DB_PASS')

    await page.locator('[id$="-file-format"]:visible').first().selectOption('json')
    await page.getByRole('button', { name: 'Save' }).click()

    await expect.poll(async () => ((await readRef(token, wsId, 'DB_ENV_FILE')).file as Record<string, unknown>).format).toBe('json')
    const edited = await readRef(token, wsId, 'DB_ENV_FILE')
    expect(edited.file).toEqual({ name: 'db.env', format: 'json', fields: ['DB_USER', 'DB_PASS'] })
    expect('field' in edited).toBe(false)
  })

  test('a template with an unknown filter is refused with the server message (#1648)', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2evaulttplbad'), {
      'execution-mode': 'agent',
    })
    await withVault(page)

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-key').fill('BAD_TEMPLATE')
    await page.locator('#var-cat').selectOption('env')
    await page.locator('#var-source').selectOption('vault')
    await page.locator('#add-mount').fill('secret')
    await page.locator('#add-path').fill('apps/x')
    await page.locator('#add-file').check()
    await page.locator('#add-file-content').selectOption('template')
    await page.locator('#add-file-template').fill('token = {{ token | upper }}')
    await page.getByRole('button', { name: 'Add Variable', exact: true }).last().click()

    // Validated when written, not when a run fails later.
    await expect(page.getByText("uses unknown filter 'upper'", { exact: false })).toBeVisible({
      timeout: 10_000,
    })
    const list = await fetch(`${API_URL}/api/v2/workspaces/${wsId}/vars`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    const keys = (await list.json()).data.map((d: { attributes: { key: string } }) => d.attributes.key)
    expect(keys).not.toContain('BAD_TEMPLATE')
  })
})

test.describe('Vault templated files — RBAC negative (regular user, #1648)', () => {
  const API_URL = process.env.API_URL || 'http://localhost:8000'
  const USER_AUTH = path.join(__dirname, '..', '.auth', 'user.json')
  test.use({ storageState: USER_AUTH })

  test('a regular user cannot write a templated Vault file to a workspace they do not own', async ({ page }) => {
    // The admin owns the workspace. A template is a request to read a secret
    // and lay its fields out in a file; being able to store one is being able
    // to ask Terrapod's Vault role for that secret, so the write must be
    // refused, not just hidden in the UI.
    const adminToken = getStoredToken()
    const wsId = await createWorkspace(adminToken, uniqueName('e2evaultrbac'), {
      'execution-mode': 'agent',
    })
    const userToken = getStoredToken('user.json')
    expect(userToken, 'the non-admin auth state must carry a token').toBeTruthy()

    const res = await fetch(`${API_URL}/api/v2/workspaces/${wsId}/vars`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${userToken}` },
      body: JSON.stringify({
        data: {
          type: 'vars',
          attributes: {
            key: 'STOLEN', category: 'env', 'value-source': 'vault',
            value: JSON.stringify({
              source: 'vault', engine: 'dynamic', mount: 'aws', path: 'creds/admin',
              file: { name: 'x', template: '{{ access_key }} {{ secret_key }}' },
            }),
          },
        },
      }),
    })
    expect([403, 404]).toContain(res.status)

    const list = await fetch(`${API_URL}/api/v2/workspaces/${wsId}/vars`, {
      headers: { Authorization: `Bearer ${adminToken}` },
    })
    const keys = (await list.json()).data.map((d: { attributes: { key: string } }) => d.attributes.key)
    expect(keys).not.toContain('STOLEN')

    // And the page offers no way to add one.
    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await expect(page.getByRole('button', { name: 'Add Variable' })).toHaveCount(0)
    await expect(page.locator('#add-file-template')).toHaveCount(0)
  })
})
