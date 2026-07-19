import { test, expect } from '@playwright/test';

// i18n language switcher (#767). The switcher is a nav-bar globe dropdown that
// writes the NEXT_LOCALE cookie (server action) and router.refresh()es so the
// server layout re-runs src/i18n/request.ts with the new cookie — no URL change,
// no full reload. These specs prove, through the real BFF proxy chain, that:
//   1. a fully-translated locale (German) actually flips visible strings and back;
//   2. another complete locale (Spanish) fully translates with no English leak;
//   3. a private-use "joke" locale (1337) renders without crashing.
// Every offered locale is complete (the completeness gate forbids partial
// locales), so a translated string is a safe cross-locale target. The English
// deep-merge remains only as a crash guard, never as a shipped fallback.

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
  // Exact match: several native names are substrings of others (e.g. "English"
  // ⊂ "English (UK)"), so a loose name match is ambiguous across 30+ locales.
  const item = page.getByRole('menuitem', { name: itemName, exact: true });
  // The switcher is a max-h-[70vh] scroll container; the bottom locales (the
  // joke set) sit well below the fold, and Playwright's implicit click-scroll
  // is unreliable inside this Radix scroll region — scroll it in explicitly.
  await item.scrollIntoViewIfNeeded();
  await item.click();
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

  test('complete locale (Spanish) fully translates, no English leak or missing keys', async ({ page }) => {
    const intlErrors = guardIntlErrors(page);

    await page.goto('/workspaces');
    await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toBeVisible();

    await switchLocale(page, /Change language/i, 'Español');

    // A complete catalog fully translates: nav + heading render in Spanish and
    // the English "Workspaces" heading is gone (no deep-merge fallback leaking).
    await expect(page.locator('nav').getByText('Espacios de trabajo').first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toHaveCount(0);

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

// The login page is the one authenticated users never see, so the nav-bar globe
// switcher isn't available there — an unauthenticated visitor would otherwise
// have no way to pick a language before signing in (#835). The login page mounts
// its own LocaleSwitcher; prove it flips the pre-sign-in copy through the BFF
// with no session.
test.describe('i18n language switcher on the login page', () => {
  // Drop the admin storageState — the login page only renders unauthenticated.
  test.use({ storageState: { cookies: [], origins: [] } });

  test('an unauthenticated visitor can choose a language before signing in', async ({ page, context }) => {
    const intlErrors = guardIntlErrors(page);

    await page.goto('/login');
    // English baseline — the sign-in subtitle above the card.
    await expect(page.getByText('Sign in to manage your infrastructure')).toBeVisible();

    // The switcher is present pre-auth and flips the copy to German.
    await switchLocale(page, /Change language/i, 'Deutsch');

    await expect(
      page.getByText('Melden Sie sich an, um Ihre Infrastruktur zu verwalten'),
    ).toBeVisible();
    await expect(page.getByText('Sign in to manage your infrastructure')).toHaveCount(0);

    // The server layout reads NEXT_LOCALE — set even without a session.
    const cookies = await context.cookies();
    expect(cookies.find((c) => c.name === 'NEXT_LOCALE')?.value).toBe('de');

    await expect(page.locator('body')).not.toContainText('MISSING_MESSAGE');
    expect(intlErrors, `next-intl errors: ${intlErrors.join('\n')}`).toEqual([]);
  });
});
