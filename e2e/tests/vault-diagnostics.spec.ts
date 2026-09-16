import { test, expect, type Route } from '@playwright/test'
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api'

const API_URL = process.env.API_URL || 'http://localhost:8000'

/**
 * Vault diagnostics (#1663): the admin status page and the reference check.
 *
 * The E2E stack runs with the Vault value source OFF, so the "disabled"
 * answers below come from the real API through the real BFF. The rich states
 * (a sealed instance, a kv-v2 key listing) need a Vault the stack does not
 * have, so those responses are stubbed with page.route — what is under test
 * there is that the UI renders every state and sends the reference as the form
 * holds it.
 */

const STATUS = {
  data: [
    {
      id: 'primary',
      type: 'vault-instance-statuses',
      attributes: {
        name: 'primary',
        default: true,
        address: 'https://vault.example.test:8200',
        namespace: 'admin',
        'auth-method': 'kubernetes',
        'auth-mount': 'kubernetes',
        'auth-role': 'terrapod',
        'tls-trust': 'instance-ca',
        reachable: true,
        initialized: true,
        sealed: true,
        standby: false,
        version: '1.18.0',
        'health-error': null,
        'login-ok': null,
        'login-error': null,
        'ttl-seconds': null,
        'checked-at': '2026-09-15T10:00:00Z',
        'last-error': {
          class: 'VaultUnavailable',
          message: "variable 'DB_PASSWORD': Vault read of 'secret/apps/db' failed with HTTP 503",
          at: '2026-09-15T09:59:00Z',
        },
      },
    },
  ],
  meta: {
    pagination: { 'current-page': 1, 'page-size': 1, 'total-count': 1, 'total-pages': 1 },
    vault: { enabled: true, 'sampled-at': '2026-09-15T10:00:00Z', 'unavailable-reason': null },
  },
}

const AVAILABLE = {
  data: {
    type: 'vault-availability',
    id: 'vault',
    attributes: { enabled: true, instances: ['primary'], 'default-instance': 'primary' },
  },
}

const json = (body: unknown) => (route: Route) =>
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

test.describe('Vault diagnostics (#1663)', () => {
  test('the admin status page shows each instance, in form as well as colour', async ({ page }) => {
    await page.route('**/api/terrapod/v1/admin/vault', json(STATUS))
    await page.goto('/admin/vault')

    const card = page.getByTestId('vault-instance-primary')
    await expect(card).toBeVisible()
    await expect(card.getByText('Reachable', { exact: true })).toBeVisible()
    await expect(card.getByText('Sealed', { exact: true })).toBeVisible()
    // A sealed Vault is never logged in to: unknown, never "failed".
    await expect(card.getByText('Login OK: Unknown')).toBeVisible()
    await expect(card.getByText('Instance CA only')).toBeVisible()
    await expect(card.getByText(/^VaultUnavailable at /)).toBeVisible()
    await expect(card.getByText(/failed with HTTP 503/)).toBeVisible()
  })

  test('with Vault off, the real endpoint says so', async ({ page }) => {
    await page.goto('/admin/vault')
    await expect(
      page.getByText('The OpenBao/Vault value source is not enabled on this deployment.'),
    ).toBeVisible()
  })

  test('the reference check posts the form as it would be saved', async ({ page }) => {
    const wsId = await createWorkspace(getStoredToken(), uniqueName('e2evaultcheck'), {
      'execution-mode': 'agent',
    })
    await page.route('**/api/terrapod/v1/vault/availability', json(AVAILABLE))
    // An object rather than a `let`: TypeScript narrows a `let` assigned only
    // inside a callback to its initial `null`.
    const captured: { body?: { data: { attributes: { reference: Record<string, unknown> } } } } = {}
    await page.route('**/vault-reference-checks', async (route) => {
      captured.body = route.request().postDataJSON()
      await json({
        data: {
          id: 'vrc-1',
          type: 'vault-reference-checks',
          attributes: {
            ok: false,
            parses: true,
            engine: 'kv2',
            keys: ['token', 'username'],
            'missing-fields': ['password'],
            notes: [],
            checks: [
              { name: 'parses', status: 'pass', detail: '' },
              { name: 'instance', status: 'pass', detail: '' },
              { name: 'path-allowed', status: 'pass', detail: '' },
              { name: 'readable', status: 'pass', detail: '' },
              { name: 'fields-present', status: 'fail', detail: 'not present: password' },
            ],
          },
        },
      })(route)
    })

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-source').selectOption('vault')
    await page.locator('#add-mount').fill('secret')
    await page.locator('#add-path').fill('apps/db')
    await page.locator('#add-field').fill('password')
    await page.getByRole('button', { name: 'Check', exact: true }).click()

    const result = page.getByRole('status')
    await expect(result.getByText('This reference will not resolve as it is.')).toBeVisible()
    await expect(result.getByText('Fields present')).toBeVisible()
    await expect(result.getByText('not present: password')).toBeVisible()
    await expect(result.getByText('token, username')).toBeVisible()
    expect(captured.body).toBeDefined()
    expect(captured.body!.data.attributes.reference).toMatchObject({
      mount: 'secret',
      path: 'apps/db',
      field: 'password',
    })
  })

  test('the reference check round-trips through the real API', async ({ page }) => {
    // Only availability is stubbed, so the form offers Vault. The check itself
    // reaches the real API, which answers that the value source is off.
    const wsId = await createWorkspace(getStoredToken(), uniqueName('e2evaultreal'), {
      'execution-mode': 'agent',
    })
    await page.route('**/api/terrapod/v1/vault/availability', json(AVAILABLE))
    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-source').selectOption('vault')
    await page.locator('#add-mount').fill('secret')
    await page.locator('#add-path').fill('apps/db')
    await page.locator('#add-field').fill('password')
    await page.getByRole('button', { name: 'Check', exact: true }).click()

    const result = page.getByRole('status')
    await expect(result.getByText('Reference is valid')).toBeVisible()
    await expect(result.getByText('The OpenBao/Vault value source is not enabled.')).toBeVisible()
  })

  test('only admin and audit may read the Vault status', async () => {
    const get = (token: string) =>
      fetch(`${API_URL}/api/terrapod/v1/admin/vault`, {
        headers: { Authorization: `Bearer ${token}` },
      })
    expect((await get(getStoredToken('user.json'))).status).toBe(403)
    expect((await get(getStoredToken('audit.json'))).status).toBe(200)
    expect((await get(getStoredToken('admin.json'))).status).toBe(200)
  })

  test('a user without variable write cannot check a reference on a workspace', async () => {
    const wsId = await createWorkspace(getStoredToken(), uniqueName('e2evaultrbac'), {
      'execution-mode': 'agent',
    })
    const res = await fetch(`${API_URL}/api/terrapod/v1/workspaces/${wsId}/vault-reference-checks`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/vnd.api+json',
        Authorization: `Bearer ${getStoredToken('user.json')}`,
      },
      body: JSON.stringify({
        data: {
          type: 'vault-reference-checks',
          attributes: { reference: { mount: 'secret', path: 'apps/db', field: 'password' } },
        },
      }),
    })
    expect(res.status).toBe(403)
  })
})
