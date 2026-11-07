/**
 * Driving the run button (#2413 F12).
 *
 * The Runs tab used to carry one button per run kind, so a spec could click
 * `Plan` by its accessible name. It is now a split button: the most complete
 * kind the workspace declares is the primary, and every partial is behind a
 * caret. So on a plain Terraform workspace `Plan + apply` is the primary and
 * `Plan` is in the closed menu — which means a locator that was correct for
 * two years now resolves to nothing.
 *
 * These helpers exist so a spec states WHICH KIND it wants and stays silent
 * about where that kind currently sits. A later change to the derivation (a new
 * kind, a different ordering) then moves one function rather than sixteen
 * locators, and a spec never encodes "Plan is the primary" as a fact.
 *
 * They deliberately do NOT assert which position a kind is in. A test that
 * cares about that — and one does, because "the loudest control is the complete
 * one" is the finding this resolves — reads `ws-run-<kind>` directly and says
 * so. See `runs.spec.ts`.
 */

import { expect, type Page } from '@playwright/test'

/** The ids `availableRunKinds` can return, in its own order. */
export type RunKind = 'full' | 'planApply' | 'configure' | 'plan'

/**
 * Reveal a run kind, opening the caret menu when it is not the primary.
 *
 * Returns the locator, so the caller can click or assert on it. The menu is
 * left open — closing it would race the click the caller is about to make.
 */
export async function revealRunKind(page: Page, kind: RunKind, timeout = 15_000) {
  const primary = page.getByTestId(`ws-run-${kind}`)
  const more = page.getByTestId('ws-run-more')

  // The primary renders whether or not a menu exists, so try it first and only
  // pay for the menu when it is genuinely needed.
  if (await primary.isVisible().catch(() => false)) return primary

  await expect(more).toBeVisible({ timeout })
  await more.click()
  await expect(page.getByTestId('ws-run-menu')).toBeVisible()
  await expect(primary).toBeVisible()
  return primary
}

/**
 * Assert a run kind is offered at all — as the primary or in the menu.
 *
 * Leaves the page as it found it: the menu is `absolute`, so at phone width
 * where the button row wraps it can sit over a control the test is about to
 * press. `revealRunKind` leaves it open because its caller is about to click
 * inside it; an assertion has no such caller, so it closes up after itself.
 */
export async function expectRunKindOffered(page: Page, kind: RunKind, timeout = 15_000) {
  const wasOpen = await page.getByTestId('ws-run-menu').isVisible().catch(() => false)
  await revealRunKind(page, kind, timeout)
  if (!wasOpen && (await page.getByTestId('ws-run-menu').isVisible().catch(() => false))) {
    await page.keyboard.press('Escape')
    await expect(page.getByTestId('ws-run-menu')).toBeHidden()
  }
}

/** Assert a run kind is offered NOWHERE — not as the primary, not in the menu. */
export async function expectRunKindAbsent(page: Page, kind: RunKind) {
  const more = page.getByTestId('ws-run-more')
  // Open the menu if there is one, so "absent" means absent from both places
  // rather than merely absent from the row. Without this the assertion passes
  // on a kind that is one click away, which is the whole hazard of a menu.
  if (await more.isVisible().catch(() => false)) {
    await more.click()
    await expect(page.getByTestId('ws-run-menu')).toBeVisible()
  }
  await expect(page.getByTestId(`ws-run-${kind}`)).toHaveCount(0)
}

/** Reveal a run kind and click it. */
export async function queueRunKind(page: Page, kind: RunKind, timeout = 15_000) {
  const target = await revealRunKind(page, kind, timeout)
  await target.click()
}
