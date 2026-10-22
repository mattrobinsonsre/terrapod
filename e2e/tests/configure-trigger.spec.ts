/**
 * How a configure STARTS, from the UI (#1974), through the real BFF proxy chain.
 *
 * Three things only a rendered page can settle, and each is a regression that
 * would be invisible to every unit test:
 *
 * The two trigger controls are DATA-gated on the configure operation being
 * declared — there is no Helm flag and no config key — so a terraform/tofu-only
 * workspace shows neither, and a regression making them unconditional would put
 * two settings that cannot do anything in front of every operator in the
 * deployment. The absence is asserted on its own, because every other test here
 * enables configure first.
 *
 * The Configure button is gated the same way, and a plan-level role must not see
 * it: a configure is apply-equivalent, so offering the button to someone the API
 * will refuse is the #1634 shape repeated for a third run kind.
 *
 * And the confirm() text is the safety surface. A triggered configure runs the
 * workspace's default limit, and an EMPTY default limit means every host in the
 * inventory — so the dialog has to say which of the two it is before anything
 * runs. Asserted by DISMISSING the dialog, so the assertion is about the warning
 * rather than about what the server then does with the run.
 */
import { test, expect, type Dialog } from '@playwright/test'
import { getStoredToken, createWorkspace, uniqueName } from '../helpers/api'

/** Capture the next confirm()'s text and DISMISS it, so nothing is queued.
 *  Registered before the click: confirm() is synchronous and blocks the
 *  handler, so a listener added afterwards never sees it. */
function captureAndDismiss(page: import('@playwright/test').Page): { text: string | null } {
  const seen: { text: string | null } = { text: null }
  page.once('dialog', async (d: Dialog) => {
    seen.text = d.message()
    await d.dismiss()
  })
  return seen
}

test.describe('Configure triggers', () => {
  test('a workspace with no configure operation shows neither trigger', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2enotrig'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    // The gate. Neither control exists, because neither can do anything
    // without `configure-enabled`.
    await expect(page.getByTestId('ws-auto-configure-enabled')).toHaveCount(0)
    await expect(page.getByTestId('ws-configure-after-apply')).toHaveCount(0)
  })

  test('the triggers appear once configure is declared, and round-trip', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2etrig'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await page.getByRole('button', { name: 'Edit', exact: true }).first().click()

    // Revealed inside the edit form as soon as configure is ticked, rather than
    // after a save: an operator turning the operation on is the one who wants
    // to say how it starts.
    await page.getByLabel('Configure', { exact: true }).check()
    await page.getByLabel('Configure on push', { exact: true }).check()
    await page.getByRole('button', { name: 'Save changes', exact: true }).click()

    await expect(page.getByTestId('ws-configure-enabled')).toHaveText('Enabled', {
      timeout: 15_000,
    })
    await expect(page.getByTestId('ws-auto-configure-enabled')).toHaveText('Enabled')
    // Off by default and left off: the two triggers are independent, and
    // turning one on must not turn the other on.
    await expect(page.getByTestId('ws-configure-after-apply')).toHaveText('Disabled')
  })

  test('the Configure button is absent without a configure operation', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2enobtn'))

    await page.goto(`/workspaces/${wsId}?tab=runs`)
    // `Plan`, not "Queue plan": the button's accessible name is
    // `phases.terraform.words.queuePlan`, whose VALUE is `Plan` — the key name
    // is not the label, and reading one off the other is what failed here.
    await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeVisible()
    await expect(
      page.getByRole('button', { name: 'Configure', exact: true }).filter({ visible: true }),
    ).toHaveCount(0)
  })

  test('the Configure button warns that an empty limit means every host', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ecfgbtn'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'site.yml',
    })

    await page.goto(`/workspaces/${wsId}?tab=runs`)
    const button = page
      .getByRole('button', { name: 'Configure', exact: true })
      .filter({ visible: true })
    await expect(button).toBeVisible({ timeout: 15_000 })

    const dialog = captureAndDismiss(page)
    await button.click()
    await expect.poll(() => dialog.text, { timeout: 10_000 }).not.toBeNull()
    // The workspace declares no default limit, so this configure would reach
    // the whole inventory — which is the thing to say before it runs.
    expect(dialog.text).toContain('WHOLE')
    expect(dialog.text).toContain('cannot be undone')
  })

  test('a declared default limit is what the warning names', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ecfglim'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'site.yml',
      'default-limit': 'canary',
    })

    await page.goto(`/workspaces/${wsId}?tab=runs`)
    const button = page
      .getByRole('button', { name: 'Configure', exact: true })
      .filter({ visible: true })
    await expect(button).toBeVisible({ timeout: 15_000 })

    const dialog = captureAndDismiss(page)
    await button.click()
    await expect.poll(() => dialog.text, { timeout: 10_000 }).not.toBeNull()
    // The contrast with the test above, and the reason both exist: a workspace
    // whose limit names a canary group cannot be widened by a trigger, and the
    // dialog has to reflect that rather than shouting about the whole estate.
    expect(dialog.text).toContain('canary')
    expect(dialog.text).not.toContain('WHOLE')
  })
})
