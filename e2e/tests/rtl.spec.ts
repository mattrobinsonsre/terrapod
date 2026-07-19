import { test, expect } from '@playwright/test';

// Right-to-left support (#829). The three RTL locales — Arabic (العربية),
// Hebrew (עברית), Persian (فارسی) — mirror the whole UI via `dir="rtl"` on
// <html> and Tailwind logical properties, while embedded code / resource
// addresses stay left-to-right through a `[dir="rtl"] code, .font-mono`
// bidi-isolation rule. This spec proves, through the real BFF proxy chain, that:
//   1. switching to Arabic flips <html dir> to "rtl" and translates the chrome
//      (no English leak, no missing-key / ICU error);
//   2. a code/identifier island stays direction:ltr under RTL (bidi isolation);
//   3. the page does not scroll horizontally when mirrored;
//   4. Hebrew and Persian also resolve to dir="rtl".

function guardIntlErrors(page: import('@playwright/test').Page) {
  const errors: string[] = [];
  page.on('console', (msg) => {
    const t = msg.text();
    if (/MISSING_MESSAGE|MISSING_TRANSLATION|INVALID_MESSAGE|IntlError/i.test(t)) errors.push(t);
  });
  page.on('pageerror', (err) => {
    if (/MISSING_MESSAGE|INVALID_MESSAGE|IntlError/i.test(String(err))) errors.push(String(err));
  });
  return errors;
}

async function switchLocale(page: import('@playwright/test').Page, triggerName: RegExp, itemName: string) {
  await page.getByRole('button', { name: triggerName }).first().click();
  // Bottom locales sit below the fold in the switcher's scroll region; scroll
  // the item in explicitly (Playwright's implicit click-scroll is flaky here).
  const item = page.getByRole('menuitem', { name: itemName, exact: true });
  await item.scrollIntoViewIfNeeded();
  await item.click();
}

test.describe('RTL locales', () => {
  test('Arabic mirrors the UI (dir=rtl), isolates code LTR, no h-scroll', async ({ page, context }) => {
    const intlErrors = guardIntlErrors(page);

    await page.goto('/workspaces');
    // LTR baseline: the default document direction and the English heading.
    await expect(page.locator('html')).toHaveAttribute('dir', 'ltr');
    await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toBeVisible();

    // Switch to Arabic via the nav globe (native name is the switcher label).
    await switchLocale(page, /Change language/i, 'العربية');

    // The whole document flips to right-to-left without a manual reload.
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
    // Chrome actually translated — the English heading is gone (no LTR leak).
    await expect(page.getByRole('heading', { name: 'Workspaces', exact: true })).toHaveCount(0);
    // The cookie the SSR layout reads is Arabic.
    const cookies = await context.cookies();
    expect(cookies.find((c) => c.name === 'NEXT_LOCALE')?.value).toBe('ar');

    // Bidi isolation: a code island (resource address) stays LTR inside the RTL
    // document, so `terraform_data.x` never scrambles in Arabic prose. Deterministic
    // — synthesises a <code> and reads its resolved direction under the live rule.
    const codeDir = await page.evaluate(() => {
      const el = document.createElement('code');
      el.textContent = 'aws_instance.web';
      document.body.appendChild(el);
      const d = getComputedStyle(el).direction;
      el.remove();
      return d;
    });
    expect(codeDir).toBe('ltr');

    // No sideways page scroll when mirrored (logical props flip cleanly).
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);

    // No missing-key / ICU leak anywhere in the rendered Arabic page.
    await expect(page.locator('body')).not.toContainText('MISSING_MESSAGE');
    await expect(page.locator('body')).not.toContainText('workspaceDetail.');
    expect(intlErrors, `next-intl errors: ${intlErrors.join('\n')}`).toEqual([]);
  });

  test('Hebrew and Persian also resolve to dir=rtl', async ({ page, context }) => {
    const intlErrors = guardIntlErrors(page);

    // Drive these two by the cookie the SSR layout reads (the switcher UI is
    // already covered by the Arabic case + i18n.spec); chaining the switcher
    // across RTL aria-labels is needlessly fragile.
    await page.goto('/workspaces');
    const origin = new URL(page.url()).origin;
    for (const loc of ['he', 'fa']) {
      await context.addCookies([{ name: 'NEXT_LOCALE', value: loc, url: origin }]);
      await page.reload();
      await expect(page.locator('html')).toHaveAttribute('dir', 'rtl');
      await expect(page.locator('body')).not.toContainText('MISSING_MESSAGE');
    }

    expect(intlErrors, `next-intl errors: ${intlErrors.join('\n')}`).toEqual([]);
  });
});
