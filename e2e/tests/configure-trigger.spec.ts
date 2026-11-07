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
import {
  revealRunKind,
  expectRunKindOffered,
  expectRunKindAbsent,
} from '../helpers/run-kinds'

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
  test('a workspace with no configure operation shows both triggers disabled', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2enotrig'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()

    // The gate is off, so neither control can do anything -- and both are
    // still RENDERED (#2413 F20). Nothing on this form is hidden by a gate: an
    // operator learns the option exists by seeing it.
    //
    // In VIEW mode a field is a value, not a control -- it renders as a span
    // reading "Disabled" -- so visibility is the whole assertion here and
    // a disabled-control assertion would be a category error. The real one is
    // asserted below, in edit mode, which is where one exists.
    for (const id of ['ws-auto-configure-enabled', 'ws-configure-after-apply']) {
      await expect(page.getByTestId(id)).toBeVisible()
    }

    // Edit mode: now they are real inputs, and they must be present AND
    // disabled. Present-and-editable is the defect that replacing a hide could
    // introduce -- a control that cannot work must not accept input.
    await page.getByRole('button', { name: 'Edit', exact: true }).first().click()
    for (const id of ['ws-auto-configure-enabled', 'ws-configure-after-apply']) {
      await expect(page.getByTestId(id)).toBeVisible()
      await expect(page.getByTestId(id)).toBeDisabled()
    }
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
    // By KIND, not by label. The run button is a split button since F12, so
    // `plan` is in the closed caret menu on a plain workspace and a locator on
    // the accessible name resolves to nothing. `expectRunKindAbsent` opens the
    // menu before asserting zero, which is the half that matters here: absent
    // from the row and one click away are different things.
    await expectRunKindOffered(page, 'plan')
    await expectRunKindAbsent(page, 'configure')
  })

  test('the Configure button does NOT prompt on a precise pointer', async ({ page }) => {
    // The decision, in the maintainer's words: *"we're not having confirm
    // prompts on those buttons on normal screens. There's time to cancel. We
    // make it hard to fat-finger and we make it clear. Our users aren't
    // children."* `Plan` and `Plan + apply` have always been this tier; the
    // configure was the outlier.
    //
    // What made it relaxable is F14 below: its reach is on the PAGE now, so the
    // information moved rather than went. The two scope assertions that used to
    // live here -- "WHOLE inventory" and "canary" -- are the F14 tests below,
    // which read the page instead of a dialog. The touch tier keeps its own
    // coverage in `responsive.spec.ts`, the only project running a coarse
    // pointer.
    //
    // Asserted by DISMISSING anything that does appear, so a regression that
    // reinstated the prompt fails here rather than hanging: an unhandled dialog
    // blocks every later Playwright action on the page.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ecfgnoprompt'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'site.yml',
    })

    await page.goto(`/workspaces/${wsId}?tab=runs`)
    // Revealed before the dialog capture is armed, because revealing opens the
    // caret menu and that is a click of its own -- arming first would leave the
    // capture watching the wrong interaction.
    const button = await revealRunKind(page, 'configure')

    // Asserted on the POST rather than on the Runs list, and that is not a
    // weakening -- it is the difference between testing this button and testing
    // the server's configuration-version requirement. A bare workspace has no
    // uploaded configuration, so queueing a configure against it answers
    // `422 Workspace has no uploaded configuration` (measured against a live
    // instance). The old version of this test DISMISSED the prompt, so it never
    // reached the POST and never met that -- relaxing the prompt is what
    // exposed it, which is the whole reason a test that blocks the action it is
    // about proves less than it looks.
    //
    // The request is also the stronger claim. "No dialog appeared" alone passes
    // on a button that does nothing at all, which is the one regression this
    // test exists to catch.
    let body: string | null = null
    await page.route('**/api/*/runs', async (route) => {
      if (route.request().method() === 'POST') body = route.request().postData()
      await route.continue()
    })

    const dialog = captureAndDismiss(page)
    await button.click()

    await expect.poll(() => body, { timeout: 15_000 }).not.toBeNull()
    expect(JSON.parse(body!).data.attributes['is-configure']).toBe(true)
    expect(dialog.text).toBeNull()
  })

  // ── the reach is on the PAGE, not only in the dialog (#2413 F14) ──────
  //
  // It used to exist only inside the confirm, so the one fact that decides
  // whether a configure touches three hosts or three hundred was visible only
  // after you had decided to press the button -- and the Limit field that sets
  // it is behind the Options panel. That is also what made the confirm
  // un-relaxable: dropping it would have removed the information rather than
  // moved it.

  test('a workspace with no default limit says so on the page, before any click', async ({
    page,
  }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ereachall'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'site.yml',
    })

    await page.goto(`/workspaces/${wsId}?tab=runs`)
    const reach = page.getByTestId('ws-configure-reach')
    await expect(reach).toBeVisible({ timeout: 15_000 })
    // The widest possible reach, and the default, so it is the loud one.
    await expect(reach).toContainText('EVERY')
  })

  test('a declared default limit is what the page names', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2ereachlim'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'site.yml',
      'default-limit': 'canary',
    })

    await page.goto(`/workspaces/${wsId}?tab=runs`)
    const reach = page.getByTestId('ws-configure-reach')
    await expect(reach).toBeVisible({ timeout: 15_000 })
    await expect(reach).toContainText('canary')
    // The contrast, and the reason both tests exist: a workspace whose limit
    // names a canary group must not be described as reaching everything.
    await expect(reach).not.toContainText('EVERY')
  })

  test('a workspace with no configure operation says nothing about reach', async ({ page }) => {
    // Data-gated on the operation, like the buttons: a terraform-only
    // workspace pays nothing for Ansible being present.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2enoreach'))

    await page.goto(`/workspaces/${wsId}?tab=runs`)
    // Anchored on the run button having rendered at all, so the absent reach
    // line below is a statement about the workspace rather than about a page
    // that had not finished loading.
    await expectRunKindOffered(page, 'plan')
    await expect(page.getByTestId('ws-configure-reach')).toHaveCount(0)
  })

})
