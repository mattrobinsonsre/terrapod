import { test, expect } from '@playwright/test';
import path from 'path';
import { gzipSync, gunzipSync } from 'zlib';
import { API_URL, getStoredToken, uniqueName, createRegistryModule, createAgentPool,
  createUser, createRole, setRoleAssignments, getSessionToken } from '../helpers/api';

/**
 * Service Catalog (#535) — drives the real catalog UI through the BFF proxy
 * chain against the real stack: admin management (provider templates + catalog
 * items), the browse + provision surfaces, and the catalog-RBAC negative
 * (a user with no catalog grant sees an empty catalog even when items exist).
 *
 * The full provision→apply→destroy→archive flow needs a real runner and is
 * covered by the live Tilt smoke (the release F-gate), not here. The
 * instance-row lifecycle actions (reconfigure / destroy / the admin-gated
 * Orphan… escape hatch) likewise need a provisioned instance, so they are
 * exercised in that live smoke rather than this stack.
 */

const USER_AUTH = path.join(__dirname, '..', '.auth', 'user.json');

function moduleArchive(input: string): Buffer {
  const content = Buffer.from(`variable "${input}" { type = string }\n`);
  const header = Buffer.alloc(512);
  header.write('main.tf');
  header.write('0000644\0', 100);
  header.write('0000000\0', 108);
  header.write('0000000\0', 116);
  header.write(content.length.toString(8).padStart(11, '0') + '\0', 124);
  header.write('00000000000\0', 136);
  header.fill(32, 148, 156);
  header.write('0', 156);
  header.write('ustar\0', 257);
  const checksum = header.reduce((sum, byte) => sum + byte, 0);
  header.write(checksum.toString(8).padStart(6, '0') + '\0 ', 148);
  return gzipSync(Buffer.concat([header, content, Buffer.alloc((512 - content.length % 512) % 512 + 1024)]));
}

test.describe('Service Catalog — admin + browse (admin session)', () => {
  test('catalog wrapper with inline provider credentials is downloadable only by platform admin', async ({ page }) => {
    const token = getStoredToken();
    const adminHeaders = { Authorization: `Bearer ${token}` };
    const moduleName = uniqueName('e2ecvmod').replaceAll('-', '');
    const templateName = uniqueName('e2ecvtemplate');
    const itemName = uniqueName('e2ecvitem');
    const poolName = uniqueName('e2ecvpool');
    const workspaceName = uniqueName('e2ecvworkspace');
    const roleName = uniqueName('e2ecvreader');
    const label = uniqueName('e2ecvscope');
    const email = `${uniqueName('e2ecvreader')}@example.com`;
    const password = 'TestPassword123!';
    const credential = uniqueName('fake-inline-credential');
    let moduleCreated = false;
    let templateId = '';
    let itemId = '';
    let poolId = '';
    let workspaceId = '';
    let userCreated = false;
    let roleCreated = false;
    try {
      const moduleId = await createRegistryModule(token, moduleName);
      moduleCreated = true;
      poolId = await createAgentPool(token, poolName);
      const template = await page.request.post('/api/v1/provider-templates', {
        headers: adminHeaders,
        data: { data: { type: 'provider-templates', attributes: {
          name: templateName, 'provider-type': 'aws',
          body: `provider "aws" { token = "${credential}" }`,
          parameters: [
            { name: 'hostname', default: 'https://example.com' },
            { name: 'token', default: credential, sensitive: true },
          ],
        } } },
      });
      expect(template.status(), await template.text()).toBe(201);
      templateId = (await template.json()).data.id;
      const item = await page.request.post('/api/v1/catalog-items', {
        headers: adminHeaders,
        data: { data: { type: 'catalog-items', attributes: {
          name: itemName, 'module-id': moduleId, 'provider-template-ids': [templateId],
          'allowed-agent-pool-ids': [poolId],
        } } },
      });
      expect(item.status(), await item.text()).toBe(201);
      itemId = (await item.json()).data.id;

      const provision = await page.request.post(`/api/v1/catalog-items/${itemId}/provision`, {
        headers: adminHeaders,
        data: { data: { type: 'catalog-instances', attributes: {
          name: workspaceName, 'agent-pool-id': poolId, labels: { team: label },
        } } },
      });
      expect(provision.status(), await provision.text()).toBe(201);
      workspaceId = (await provision.json()).data.relationships.workspace.data.id;

      await createUser(token, email, password);
      userCreated = true;
      await createRole(token, roleName, ['workspace:read', 'config:read', 'var:read'], { team: label });
      roleCreated = true;
      await setRoleAssignments(token, email, [roleName]);
      const { token: readerToken, roles } = await getSessionToken(email, password);
      expect(roles).toContain(roleName);
      expect(roles).not.toContain('admin');
      const readerHeaders = { Authorization: `Bearer ${readerToken}` };
      const varsResponse = await page.request.get(`/api/v1/workspaces/${workspaceId}/vars`, { headers: readerHeaders });
      expect(varsResponse.status(), await varsResponse.text()).toBe(200);
      const vars = (await varsResponse.json()).data;
      const values = Object.fromEntries(vars.map((row: { attributes: { key: string } }) => [row.attributes.key, row.attributes]));
      expect(values.hostname).toMatchObject({ value: 'https://example.com', sensitive: false });
      expect(values.token).toMatchObject({ value: null, sensitive: true, 'version-id': null });
      expect(JSON.stringify(vars)).not.toContain(credential);

      await page.goto(`/workspaces/${workspaceId}?tab=variables`);
      await expect(page.locator('tr', { hasText: 'hostname' })).toContainText('https://example.com');
      await expect(page.locator('tr', { hasText: 'token' }).first()).toContainText('***');

      const versionsUrl = `/api/v1/workspaces/${workspaceId}/configuration-versions`;
      const readerVersions = await page.request.get(versionsUrl, { headers: readerHeaders });
      expect(readerVersions.status(), await readerVersions.text()).toBe(200);
      const versions = (await readerVersions.json()).data;
      expect(versions).toHaveLength(1);
      expect(versions[0].attributes).toMatchObject({ source: 'catalog', status: 'uploaded' });
      expect(JSON.stringify(versions)).not.toContain(credential);
      const cvId = versions[0].id;
      const cvUrl = `/api/v1/configuration-versions/${cvId}`;
      const metadata = await page.request.get(cvUrl, { headers: readerHeaders });
      expect(metadata.status(), await metadata.text()).toBe(200);
      expect(await metadata.text()).not.toContain(credential);

      for (const suffix of ['download', 'download-ticket']) {
        const url = `${cvUrl}/${suffix}`;
        const denied = suffix === 'download'
          ? await page.request.get(url, { headers: readerHeaders })
          : await page.request.post(url, { headers: readerHeaders });
        expect(denied.status(), await denied.text()).toBe(404);
        expect(await denied.text()).not.toContain(credential);
      }

      const direct = await page.request.get(`${cvUrl}/download`, { headers: adminHeaders });
      expect(direct.status(), await direct.text()).toBe(200);
      const wrapper = gunzipSync(await direct.body()).toString('utf-8');
      expect(wrapper).toContain('providers.tf');
      expect(wrapper).toContain(`token = "${credential}"`);

      const minted = await page.request.post(`${cvUrl}/download-ticket`, { headers: adminHeaders });
      expect(minted.status(), await minted.text()).toBe(200);
      const ticketUrl = (await minted.json()).data.attributes.url as string;
      expect(ticketUrl).toMatch(/^\/api\/v1\/configuration-versions\/download-by-ticket\//);
      const ticketDownload = await page.request.get(ticketUrl);
      expect(ticketDownload.status(), await ticketDownload.text()).toBe(200);
      expect(gunzipSync(await ticketDownload.body()).toString('utf-8')).toContain(credential);
    } finally {
      if (workspaceId) expect((await page.request.delete(`/api/v1/catalog-instances/${workspaceId}?orphan=true`, { headers: adminHeaders })).status()).toBe(204);
      if (itemId) expect((await page.request.delete(`/api/v1/catalog-items/${itemId}`, { headers: adminHeaders })).status()).toBe(204);
      if (templateId) expect((await page.request.delete(`/api/v1/provider-templates/${templateId}`, { headers: adminHeaders })).status()).toBe(204);
      if (moduleCreated) expect((await page.request.delete(`/api/v1/registry-modules/private/default/${moduleName}/aws`, { headers: adminHeaders })).status()).toBe(204);
      if (poolId) expect((await page.request.delete(`/api/v1/agent-pools/${poolId}`, { headers: adminHeaders })).status()).toBe(204);
      if (userCreated) expect((await page.request.delete(`/api/v1/users/${email}`, { headers: adminHeaders })).status()).toBe(204);
      if (roleCreated) expect((await page.request.delete(`/api/v1/roles/${roleName}`, { headers: adminHeaders })).status()).toBe(204);
    }
  });

  test('BFF redacts catalog authoring values by default and admin editor preserves them', async ({ page }) => {
    const token = getStoredToken('admin.json');
    const userToken = getStoredToken('user.json');
    const moduleName = uniqueName('e2eredact').replaceAll('-', '');
    const moduleId = await createRegistryModule(token, moduleName, 'aws');
    const itemName = uniqueName('e2eredact-item');
    const templateName = uniqueName('e2eredact-template');
    const secret = uniqueName('e2esecret');
    const adminHeaders = { Authorization: `Bearer ${token}` };
    let itemId = '';
    let templateId = '';
    try {
      const template = await page.request.post('/api/v1/provider-templates', {
        headers: adminHeaders,
        data: { data: { attributes: { name: templateName, 'provider-type': 'aws',
          body: `provider "aws" { token = "${secret}" }`,
          parameters: [{ name: 'token', sensitive: true, default: secret }],
        } } },
      });
      expect(template.ok(), await template.text()).toBeTruthy();
      const templateCreated = await template.json();
      expect(JSON.stringify(templateCreated)).not.toContain(secret);
      templateId = templateCreated.data.id;
      const item = await page.request.post('/api/v1/catalog-items', {
        headers: adminHeaders,
        data: { data: { attributes: { name: itemName, 'module-id': moduleId,
          'variable-options': [{ name: 'token', hidden: true, default: secret }],
        } } },
      });
      expect(item.ok(), await item.text()).toBeTruthy();
      const itemCreated = await item.json();
      expect(JSON.stringify(itemCreated)).not.toContain(secret);
      itemId = itemCreated.data.id;

      for (const url of [`/api/v1/provider-templates/${templateId}`, `/api/v1/catalog-items/${itemId}`]) {
        const ordinary = await page.request.get(url, { headers: adminHeaders });
        expect(ordinary.ok()).toBeTruthy();
        expect(await ordinary.text()).not.toContain(secret);
        const denied = await page.request.get(`${url}?include-sensitive-values=true`, {
          headers: { Authorization: `Bearer ${userToken}` },
        });
        expect(denied.status()).toBe(403);
        const full = await page.request.get(`${url}?include-sensitive-values=true`, { headers: adminHeaders });
        expect(full.ok()).toBeTruthy();
        expect(await full.text()).toContain(secret);
      }
      const templates = await page.request.get('/api/v1/provider-templates', { headers: adminHeaders });
      expect(await templates.text()).not.toContain(secret);
      const items = await page.request.get('/api/v1/catalog-items', { headers: adminHeaders });
      expect(await items.text()).not.toContain(secret);

      await page.goto('/admin/provider-templates');
      await page.locator('tr', { hasText: templateName }).getByRole('button', { name: 'Edit' }).click();
      await expect(page.locator('#pt-body')).toHaveValue(new RegExp(secret));
      await expect(page.locator('#pt-params')).toHaveValue(new RegExp(secret));
      await page.getByRole('button', { name: 'Save Changes' }).click();
      await expect(page.getByText(`Provider template "${templateName}" updated`)).toBeVisible();

      await page.goto('/admin/catalog');
      await page.locator('tr', { hasText: itemName }).getByRole('button', { name: 'Edit' }).click();
      await expect(page.locator('#cat-varopts')).toHaveValue(new RegExp(secret));
      await page.getByRole('button', { name: 'Save Changes' }).click();
      const saved = await page.request.get(`/api/v1/catalog-items/${itemId}?include-sensitive-values=true`, { headers: adminHeaders });
      expect((await saved.json()).data.attributes['variable-options'][0].default).toBe(secret);
    } finally {
      if (itemId) expect((await page.request.delete(`/api/v1/catalog-items/${itemId}`, { headers: adminHeaders })).status()).toBe(204);
      if (templateId) expect((await page.request.delete(`/api/v1/provider-templates/${templateId}`, { headers: adminHeaders })).status()).toBe(204);
      expect((await page.request.delete(`/api/v1/registry-modules/private/default/${moduleName}/aws`, { headers: adminHeaders })).status()).toBe(204);
    }
  });

  test('provider template: create and see it listed', async ({ page }) => {
    const name = uniqueName('e2e-pt');
    await page.goto('/admin/provider-templates');
    await expect(page.locator('h1:has-text("Provider Templates")')).toBeVisible();

    await page.click('button:has-text("New Template")');
    await page.fill('#pt-name', name);
    await page.fill('#pt-type', 'aws');
    await page.fill('#pt-body', 'provider "aws" {}');
    await page.click('button[type="submit"]:has-text("Create Template")');

    // Scope to the list row — a page-wide text match also hits the
    // "Provider template "<name>" created" success banner (strict-mode: 2 elements).
    await expect(page.locator(`td:has-text("${name}")`)).toBeVisible({ timeout: 10_000 });
  });

  test('catalog item: create over a module, browse, render provision panel', async ({ page }) => {
    const token = getStoredToken('admin.json');
    // Module names are restricted to a registry-safe charset — no hyphens.
    const moduleName = `e2ecatmod${Date.now().toString(36)}`;
    const moduleId = await createRegistryModule(token, moduleName, 'aws');
    const itemName = uniqueName('e2ecat');

    // Admin create
    await page.goto('/admin/catalog');
    await expect(page.locator('h1:has-text("Catalog Admin")')).toBeVisible();
    await page.click('button:has-text("New Catalog Item")');
    await page.fill('#cat-name', itemName);
    // Select the module by its UUID value (robust against label formatting).
    await page.selectOption('#cat-module', moduleId);
    await page.click('button[type="submit"]:has-text("Create Catalog Item")');
    // List row, not a page-wide text match (avoids also hitting the success banner).
    await expect(page.locator(`td:has-text("${itemName}")`)).toBeVisible({ timeout: 10_000 });

    // Browse — the enabled item appears
    await page.goto('/catalog');
    await expect(page.locator('h1:has-text("Service Catalog")')).toBeVisible();
    await expect(page.locator(`text=${itemName}`)).toBeVisible({ timeout: 10_000 });

    // Detail — provision panel renders
    await page.route('**/api/terrapod/v1/catalog-items/*/form', (route) => route.fulfill({ json: {
      data: { attributes: { 'resolved-version': null, fields: [
        { name: 'hostname', description: 'The runner-reachable address.', required: true, sensitive: false, default: null, options: null, source: 'module' },
        { name: 'token', description: '', required: true, sensitive: true, default: null, options: null, source: 'module' },
      ] } },
    } }));
    await page.click(`a:has-text("${itemName}")`);
    await expect(page.locator('h2:has-text("Provision")')).toBeVisible({ timeout: 10_000 });
    // Selection-first: the agent-pool select is present (not a free-text field).
    await expect(page.locator('select').first()).toBeVisible();
    const hostname = page.locator('#prov-hostname');
    const tokenInput = page.locator('#prov-token');
    await expect(hostname).toBeVisible();
    await expect(tokenInput).toBeVisible();
    await expect(page.locator('label[for="prov-token"]')).toContainText('sensitive');
    const hostnameBox = await hostname.boundingBox();
    const tokenBox = await tokenInput.boundingBox();
    expect(hostnameBox).not.toBeNull();
    expect(tokenBox).not.toBeNull();
    expect(Math.abs(hostnameBox!.y - tokenBox!.y)).toBeLessThan(2);

    // Module interface (#1585): read from /catalog-items/{id}/interface through
    // the BFF. This module has no uploaded version yet, so the interface is
    // empty — the section still renders, and says so when opened.
    const iface = page.getByRole('button', { name: /Inputs & Outputs/ });
    await expect(iface).toBeVisible({ timeout: 10_000 });
    await expect(iface).toContainText('0 inputs, 0 outputs');
    const noData = page.getByText('No interface data available for this version.');
    await expect(async () => {
      if (!(await noData.isVisible())) await iface.click();
      await expect(noData).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });
  });

  test('admin switches a wrapper to a self-contained item and clears constraints again', async ({ page }) => {
    const token = getStoredToken();
    const moduleName = uniqueName('e2ecatroot').replaceAll('-', '');
    const moduleId = await createRegistryModule(token, moduleName, 'aws');
    const itemName = uniqueName('e2ecat-root');
    const templateName = uniqueName('e2ecat-template');
    let itemId = '';
    let templateId = '';
    try {
      const createdTemplate = await fetch(`${API_URL}/api/v1/provider-templates`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'provider-templates', attributes: {
          name: templateName, 'provider-type': 'aws', body: 'provider "aws" {}',
        } } }),
      });
      if (!createdTemplate.ok) throw new Error(`Provider template creation failed: ${createdTemplate.status} ${await createdTemplate.text()}`);
      templateId = (await createdTemplate.json()).data.id;

      await page.goto('/admin/catalog');
      await page.getByRole('button', { name: 'New Catalog Item' }).click();
      await page.locator('#cat-name').fill(itemName);
      await page.locator('#cat-module').selectOption(moduleId);
      await page.getByRole('button', { name: new RegExp(templateName) }).click();
      const createResponse = page.waitForResponse((response) => response.url().endsWith('/catalog-items') && response.request().method() === 'POST');
      await page.getByRole('button', { name: 'Create Catalog Item' }).click();
      itemId = (await (await createResponse).json()).data.id;
      const row = page.locator('tr', { hasText: itemName });
      await expect(row).toBeVisible();

      await row.getByRole('button', { name: 'Edit' }).click();
      await page.getByLabel('Self-contained root archive').check();
      await expect(page.getByText('Saving removes existing provider-template associations.')).toBeVisible();
      await page.getByRole('button', { name: 'Save Changes' }).click();
      await expect(page.getByText('Select at least one execution backend.')).toBeVisible();
      await page.getByLabel('OpenTofu', { exact: true }).check();
      await page.locator('#cat-tofu-range').fill('>=1.12,<2');
      await page.getByRole('button', { name: 'Save Changes' }).click();
      await expect(page.getByText(`Catalog item "${itemName}" updated`)).toBeVisible();
      // Checking through the BFF also verifies the editor's payload reached the API unchanged.
      const saved = await page.request.get(`/api/terrapod/v1/catalog-items/${itemId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(saved.ok()).toBeTruthy();
      expect((await saved.json()).data.attributes).toMatchObject({
        'self-contained': true,
        'execution-backend-constraints': { tofu: '>=1.12,<2' },
        'provider-template-ids': [],
      });

      await row.getByRole('button', { name: 'Edit' }).click();
      await expect(page.getByLabel('Self-contained root archive')).toBeChecked();
      await expect(page.getByLabel('OpenTofu', { exact: true })).toBeChecked();
      await expect(page.locator('#cat-tofu-range')).toHaveValue('>=1.12,<2');
      await page.getByLabel('Self-contained root archive').uncheck();
      await page.getByLabel('Restrict execution backends').uncheck();
      const clearResponse = page.waitForResponse((response) => response.url().endsWith(`/catalog-items/${itemId}`) && response.request().method() === 'PATCH');
      await page.getByRole('button', { name: 'Save Changes' }).click();
      expect((await clearResponse).ok()).toBeTruthy();
      const cleared = await page.request.get(`/api/terrapod/v1/catalog-items/${itemId}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(cleared.ok()).toBeTruthy();
      expect((await cleared.json()).data.attributes).toMatchObject({
        'self-contained': false, 'execution-backend-constraints': null, 'provider-template-ids': [],
      });
    } finally {
      if (itemId) {
        const remove = await fetch(`${API_URL}/api/v1/catalog-items/${itemId}`, {
          method: 'DELETE', headers: { Authorization: `Bearer ${token}` },
        });
        expect(remove.status).toBe(204);
      }
      if (templateId) {
        const remove = await fetch(`${API_URL}/api/v1/provider-templates/${templateId}`, {
          method: 'DELETE', headers: { Authorization: `Bearer ${token}` },
        });
        expect(remove.status).toBe(204);
      }
      const removeModule = await fetch(
        `${API_URL}/api/v1/registry-modules/private/default/${moduleName}/aws`,
        { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } },
      );
      expect(removeModule.status).toBe(204);
    }
  });

  test('admin creates a self-contained item with a Terraform-only version range', async ({ page }) => {
    const token = getStoredToken();
    const moduleName = uniqueName('e2erootcreate').replaceAll('-', '');
    const moduleId = await createRegistryModule(token, moduleName, 'aws');
    const itemName = uniqueName('e2eroot');
    let itemId = '';
    try {
      await page.goto('/admin/catalog');
      await page.getByRole('button', { name: 'New Catalog Item' }).click();
      await page.locator('#cat-name').fill(itemName);
      await page.locator('#cat-module').selectOption(moduleId);
      await page.getByLabel('Self-contained root archive').check();
      await page.getByLabel('Terraform', { exact: true }).check();
      await page.locator('#cat-terraform-range').fill('>=1.6,<2');
      const createResponse = page.waitForResponse((response) => response.url().endsWith('/catalog-items') && response.request().method() === 'POST');
      await page.getByRole('button', { name: 'Create Catalog Item' }).click();
      itemId = (await (await createResponse).json()).data.id;
      const row = page.locator('tr', { hasText: itemName });
      await expect(row.getByText('self-contained')).toBeVisible();
      const list = await page.request.get('/api/terrapod/v1/catalog-items', {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(list.ok()).toBeTruthy();
      const item = (await list.json()).data.find((entry: { attributes: { name: string } }) => entry.attributes.name === itemName);
      itemId = item.id;
      expect(item.attributes).toMatchObject({
        'self-contained': true,
        'execution-backend-constraints': { terraform: '>=1.6,<2' },
        'provider-template-ids': [],
      });
    } finally {
      if (itemId) {
        const remove = await fetch(`${API_URL}/api/v1/catalog-items/${itemId}`, {
          method: 'DELETE', headers: { Authorization: `Bearer ${token}` },
        });
        expect(remove.status).toBe(204);
      }
      const removeModule = await fetch(
        `${API_URL}/api/v1/registry-modules/private/default/${moduleName}/aws`,
        { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } },
      );
      expect(removeModule.status).toBe(204);
    }
  });

  test('reconfigure text retains focus while typing and version selection refreshes inputs', async ({ page }) => {
    const token = getStoredToken();
    const moduleName = uniqueName('e2ecatversion').replaceAll('-', '');
    const moduleId = await createRegistryModule(token, moduleName, 'aws');
    const itemName = uniqueName('e2ecat-version');
    let itemId = '';
    try {
      for (const [version, input] of [['1.0.0', 'old_input'], ['2.0.0', 'new_input']]) {
        const response = await fetch(
          `${API_URL}/api/terrapod/v1/registry-modules/private/default/${moduleName}/aws/versions/${version}/upload`,
          { method: 'PUT', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/octet-stream' }, body: new Uint8Array(moduleArchive(input)) },
        );
        expect(response.ok, await response.text()).toBeTruthy();
      }

      const create = await fetch(`${API_URL}/api/v1/catalog-items`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'catalog-items', attributes: {
          name: itemName, 'module-id': moduleId, 'default-version-pin': '1.0.0',
        } } }),
      });
      if (!create.ok) throw new Error(`Catalog item creation failed: ${create.status} ${await create.text()}`);
      itemId = (await create.json()).data.id;
      const wsId = '0198e2e0-0000-7000-8000-00000000c099';
      const base = `/api/terrapod/v1/catalog-items/${itemId}`;
      const list = { data: [{ id: wsId, attributes: { name: 'versioned-instance',
        'catalog-version-pin': '1.0.0', 'agent-pool-id': null, labels: {},
        'catalog-update-block': { version: '2.0.0', reason: 'inputs' },
      } }], meta: { pagination: { 'current-page': 1, 'page-size': 1, 'total-count': 1, 'total-pages': 1 } } };
      // The form requests run through the real BFF and API. Only the instance
      // is stubbed: this test needs saved values but does not need a runner.
      await page.route((url) => url.pathname === `${base}/instances`, (route) => route.fulfill({ json: list }));
      await page.route((url) => url.pathname === `/api/terrapod/v1/catalog-instances/${wsId}`, (route) => {
        if (route.request().method() === 'GET') return route.fulfill({ json: {
          data: { id: wsId, attributes: { ...list.data[0].attributes,
            'input-values': { old_input: 'saved' },
          } },
        } });
        return route.fulfill({ status: 422, json: { detail: 'test does not create a workspace' } });
      });
      await page.goto(`/catalog/${itemId}`);
      await expect(page.locator('#prov-old_input')).toBeVisible();
      await expect(page.getByText('Version 2.0.0 needs reconfiguration before it can run.')).toBeVisible();
      await page.selectOption('#prov-version', '');
      await expect(page.locator('#prov-new_input')).toBeVisible();
      await expect(page.locator('#prov-old_input')).toHaveCount(0);
      await page.getByRole('button', { name: 'Reconfigure' }).click();
      await expect(page.getByText('Version 2.0.0 needs reconfiguration before it can run.')).toHaveCount(2);
      await expect(page.locator('#recfg-old_input')).toHaveValue('saved');
      const editedInput = page.locator('#recfg-old_input');
      await editedInput.focus();
      await page.keyboard.type('x');
      await expect(editedInput).toBeFocused();
      await page.keyboard.type('yz');
      await expect(editedInput).toHaveValue('savedxyz');
      await expect(editedInput).toBeFocused();
      await page.selectOption('#recfg-version', '');
      await expect(page.locator('#recfg-new_input')).toBeVisible();
      await expect(page.locator('#recfg-old_input')).toHaveCount(0);
      await expect(page.getByText('No longer used by this version')).toBeVisible();
      await expect(page.getByText('old_input', { exact: true })).toBeVisible();
      await page.locator('#recfg-new_input').fill('replacement');
      const patch = page.waitForRequest((request) => request.url().endsWith(`/catalog-instances/${wsId}`) && request.method() === 'PATCH');
      await page.getByRole('button', { name: 'Queue run' }).click();
      expect((await patch).postDataJSON().data.attributes).toMatchObject({
        'version-pin': null, 'input-values': { new_input: 'replacement' }, 'auto-apply': false,
      });
      list.data[0].attributes['catalog-update-block'] = { version: '2.0.0', reason: 'runtime' };
      await page.reload();
      await expect(page.getByText("Version 2.0.0 cannot run with this workspace's backend and version. Select a compatible runtime through the API, then reconfigure.")).toBeVisible();
    } finally {
      if (itemId) {
        const remove = await fetch(`${API_URL}/api/v1/catalog-items/${itemId}`, {
          method: 'DELETE', headers: { Authorization: `Bearer ${token}` },
        });
        expect(remove.status).toBe(204);
      }
      const removeModule = await fetch(
        `${API_URL}/api/v1/registry-modules/private/default/${moduleName}/aws`,
        { method: 'DELETE', headers: { Authorization: `Bearer ${token}` } },
      );
      expect(removeModule.status).toBe(204);
    }
  });
});

test.describe('Service Catalog — RBAC negative (regular user)', () => {
  test.use({ storageState: USER_AUTH });

  test('a user with no catalog grant sees an empty catalog', async ({ page }) => {
    await page.goto('/catalog');
    await expect(page.locator('h1:has-text("Service Catalog")')).toBeVisible();
    // Even though admin items exist in the shared DB, catalog RBAC filters them
    // out for a user with no catalog permission — the page shows the empty
    // state, never another user's item.
    await expect(page.locator('text=No catalog items available.')).toBeVisible({
      timeout: 10_000,
    });
  });
});
