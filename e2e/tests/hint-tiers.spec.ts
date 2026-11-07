/**
 * Two-tier field hints, through the real BFF proxy chain (#2413 F15).
 *
 * Five things only a rendered page can settle, and each is the reason this
 * component exists rather than another `title=`:
 *
 * The detail OPENS ON CLICK and not on hover. That is a decision, in the
 * maintainer's words — *"yes always click. I don't like hover."* — and it is
 * also the accessibility rule AGENTS.md states outright: a hover tooltip is
 * unreachable on touch and invisible to a keyboard, and the native `title`
 * attribute is both. A regression to hover would pass every unit test, because
 * the registry and the render gate both only ask whether a lead EXISTS.
 *
 * It is reachable by KEYBOARD, which is the half a `title` can never satisfy.
 *
 * Each toggle controls its OWN detail. These render many to a form, so a
 * single piece of shared open-state would expand all of them at once — a
 * defect that looks fine in a screenshot of one field.
 *
 * And the LEAD is always visible: the point of the split is that an operator
 * never has to open anything to learn what a control does, only to learn how
 * it works. A regression that hid the lead behind the icon too would turn
 * every hint into a thing you have to go and find.
 */
import { test, expect } from '@playwright/test'

/** The create form, which carries several tiered hints (`allowForkPrPlans.hint`,
 *  `oidcAudiencesHint`, `driftIgnoreRulesHint`, the Ansible ones). Chosen over
 *  the detail page because it needs no workspace fixture, so the assertions are
 *  about the component rather than about seeding. */
async function openCreateForm(page: import('@playwright/test').Page) {
  await page.goto('/workspaces')
  // `New Workspace` OPENS the form; `Create Workspace` is the submit button
  // inside it, so clicking that first waits for a control that does not exist
  // yet. Copied from the wrong line of `workspaces.spec.ts` the first time.
  await page.click('button:has-text("New Workspace")')
  const toggles = page.getByTestId('hint-detail-toggle')
  // A positive count FIRST: every assertion below is about "one of these", and
  // a zero would make all of them vacuously true -- the repository's most
  // repeated gate defect, arriving through a locator.
  await expect(toggles.first()).toBeVisible({ timeout: 15_000 })
  return toggles
}

test.describe('Two-tier hints', () => {
  test('the lead is visible and the detail is closed until asked for', async ({ page }) => {
    const toggles = await openCreateForm(page)
    const n = await toggles.count()
    expect(n).toBeGreaterThan(1)

    // Nothing is expanded on arrival: the form is not a wall of text.
    await expect(page.getByTestId('hint-detail')).toHaveCount(0)
    for (let i = 0; i < n; i++) {
      await expect(toggles.nth(i)).toHaveAttribute('aria-expanded', 'false')
    }
  })

  test('a click opens exactly one detail, and a second click closes it', async ({ page }) => {
    const toggles = await openCreateForm(page)
    const first = toggles.first()

    await first.click()
    await expect(first).toHaveAttribute('aria-expanded', 'true')
    // Exactly one -- not "at least one". Shared open-state is the defect.
    await expect(page.getByTestId('hint-detail')).toHaveCount(1)
    const detail = page.getByTestId('hint-detail').first()
    await expect(detail).toBeVisible()
    // `aria-controls` points at the block that actually appeared, so a screen
    // reader is told where the new content is rather than having to find it.
    const controls = await first.getAttribute('aria-controls')
    expect(controls).toBeTruthy()
    await expect(detail).toHaveAttribute('id', controls!)

    await first.click()
    await expect(first).toHaveAttribute('aria-expanded', 'false')
    await expect(page.getByTestId('hint-detail')).toHaveCount(0)
  })

  test('each toggle opens its own detail, not everybody else as well', async ({ page }) => {
    const toggles = await openCreateForm(page)
    await toggles.nth(0).click()
    await toggles.nth(1).click()
    await expect(page.getByTestId('hint-detail')).toHaveCount(2)
    await expect(toggles.nth(0)).toHaveAttribute('aria-expanded', 'true')
    await expect(toggles.nth(1)).toHaveAttribute('aria-expanded', 'true')

    // Closing one leaves the other open -- the state is per control.
    await toggles.nth(0).click()
    await expect(page.getByTestId('hint-detail')).toHaveCount(1)
    await expect(toggles.nth(1)).toHaveAttribute('aria-expanded', 'true')
  })

  test('the keyboard opens it, which a hover title could never do', async ({ page }) => {
    const toggles = await openCreateForm(page)
    const first = toggles.first()

    await first.focus()
    await expect(first).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(first).toHaveAttribute('aria-expanded', 'true')

    await page.keyboard.press('Space')
    await expect(first).toHaveAttribute('aria-expanded', 'false')
  })

  test('hovering does NOT open it, and it carries no explanatory title', async ({ page }) => {
    const toggles = await openCreateForm(page)
    const first = toggles.first()

    await first.hover()
    // Give a hover handler every chance to fire before concluding it has none:
    // asserting immediately would pass against a tooltip with any delay at all.
    await expect(page.getByTestId('hint-detail')).toHaveCount(0)
    await expect(first).toHaveAttribute('aria-expanded', 'false')

    // And the explanation is not smuggled back in as a `title`, which is the
    // exact attribute this component replaced. The accessible name is a label
    // ("Show the full explanation"), never the explanation itself.
    await expect(first).not.toHaveAttribute('title', /.+/)
  })
})
