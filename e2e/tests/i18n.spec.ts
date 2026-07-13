import { test, expect } from '@playwright/test';

// i18n language switcher (#767). The switcher is a nav-bar globe dropdown that
// writes the NEXT_LOCALE cookie (server action) and router.refresh()es so the
// server layout re-runs src/i18n/request.ts with the new cookie — no URL change,
// no full reload. These specs prove, through the real BFF proxy chain, that:
//   1. a fully-translated locale (German) actually flips visible strings and back;
//   2. a partially-translated locale (Spanish) renders with the English deep-merge
//      fallback and never leaks a MISSING_MESSAGE / raw key;
//   3. a private-use "joke" locale (1337) renders without crashing.
// German is the reliably-complete catalog; the partial catalogs at minimum
// translate the `nav` namespace, so nav labels are a safe cross-locale target.

// Fail the test if next-intl logs a missing-message / ICU error to the console —
// a broken placeholder or absent key would surface here.
function guardIntlErrors(page: import('@playwright/test').Page) {
  const errors: string[] = [];
  page.on('console', (msg) => {
    const t = msg.text();
    if (/MISSING_MESSAGE|MISSING_TRANSLATION|INVALID_MESSAGE|IntlError/i.test(t)) {
      errors.push(t);
    }
  });
  page.on('pageerror', (err) => {
    if (/MISSING_MESSAGE|INVALID_MESSAGE|IntlError/i.test(String(err))) {
      errors.push(String(err));
    }
  });
  return errors;
}

async function switchLocale(page: import('@playwright/test').Page, triggerName: RegExp, itemName: string) {
  await page.getByRole('button', { name: triggerName }).first().click();
  await page.getByRole('menuitem', { name: itemName }).click();
}

test.describe('i18n language switcher', () => {
  test('German flips strings through the BFF and back', async ({ page, context }) => {
    const intlErrors = guardIntlErrors(page);

    await page.goto('/workspaces');
    // English baseline (default locale — storageState carries no NEXT_LOCALE).
    await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toBeVisible();

    // Switch to Deutsch. Trigger's accessible name is the translated aria-label
    // ("Change language" in EN), the item is the native name "Deutsch".
    await switchLocale(page, /Change language/i, 'Deutsch');

    // The heading and nav re-render in German without a manual reload.
    await expect(page.getByRole('heading', { name: 'Arbeitsbereiche', exact: true })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toHaveCount(0);
    await expect(page.locator('nav').getByText('Arbeitsbereiche').first()).toBeVisible();

    // The cookie the server layout reads is set to the chosen locale.
    const cookies = await context.cookies();
    expect(cookies.find((c) => c.name === 'NEXT_LOCALE')?.value).toBe('de');

    // No missing-key / ICU leak anywhere in the rendered German page.
    await expect(page.locator('body')).not.toContainText('MISSING_MESSAGE');
    await expect(page.locator('body')).not.toContainText('workspaceDetail.');

    // Switch back — trigger's aria-label is now German ("Sprache ändern").
    await switchLocale(page, /Sprache ändern/i, 'English');
    await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toBeVisible();

    expect(intlErrors, `next-intl errors: ${intlErrors.join('\n')}`).toEqual([]);
  });

  test('partial locale (Spanish) renders with English fallback, no missing keys', async ({ page }) => {
    const intlErrors = guardIntlErrors(page);

    await page.goto('/workspaces');
    await switchLocale(page, /Change language/i, 'Español');

    // The `nav` namespace IS translated in the partial catalog → visible change.
    await expect(page.locator('nav').getByText('Espacios de trabajo').first()).toBeVisible();
    // The page heading is NOT in the partial catalog → deep-merge falls back to
    // English ("Workspaces"), proving a half-translated locale stays renderable.
    await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toBeVisible();

    await expect(page.locator('body')).not.toContainText('MISSING_MESSAGE');
    expect(intlErrors, `next-intl errors: ${intlErrors.join('\n')}`).toEqual([]);
  });

  test('private-use joke locale (1337) renders without crashing', async ({ page }) => {
    const intlErrors = guardIntlErrors(page);

    await page.goto('/workspaces');
    await switchLocale(page, /Change language/i, '1337 5p34k');

    await expect(page.locator('nav').getByText('W0rk5p4c35').first()).toBeVisible();
    await expect(page.locator('body')).not.toContainText('MISSING_MESSAGE');
    expect(intlErrors, `next-intl errors: ${intlErrors.join('\n')}`).toEqual([]);
  });
});
