import { test, expect, type Page, type Route } from '@playwright/test';
// Lives in helpers/, not here: Playwright forbids a spec importing a spec, and
// any suite adding a surface should be able to reuse the mobile guard.
import { expectNoHorizontalPageScroll } from '../helpers/responsive';
import { getStoredToken, createWorkspace, lockWorkspace, createUser, createAgentPool, createRegistryModule, seedRun, seedStateVersion, seedStateVersionWithContent, seedRunTask, uniqueName } from '../helpers/api';

const API_URL = process.env.API_URL || 'http://localhost:8000';

/**
 * Responsive / mobile harness (#719).
 *
 * This project runs at a phone viewport (see the `responsive` project in
 * playwright.config.ts — a Pixel device descriptor). It is the "mobile"
 * half of the two-sided testing contract: this suite proves the UI works
 * at phone width, while the existing desktop projects prove the desktop
 * view is unchanged (the desktop guard). One DRY UI, adapted by width —
 * never a forked mobile build, never user-agent sniffing.
 *
 * Per-page assertions (no horizontal page scroll, tables reflow, tab
 * survives reload, log tail visible, …) are added to this suite as each
 * stage of #719 fixes the corresponding surface, so the guard grows with
 * the work and can't silently regress.
 */

test.describe('Responsive harness (phone viewport)', () => {
  test('the Vault reference builder is usable at phone width (#1439)', async ({ page }) => {
    // The desktop defect this guards against in the other direction: five
    // fields crammed into a narrow container truncated the path and field
    // inputs to unreadable stubs. The same component renders in the mobile
    // card, so it has to hold up here too.
    const token = getStoredToken()
    // Agent mode: a vault reference only resolves on the listener claim path,
    // so the source picker is not offered on a local workspace.
    const wsId = await createWorkspace(token, uniqueName('e2erespvault'), {
      'execution-mode': 'agent',
    })

    await page.route('**/api/terrapod/v1/vault/availability', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: {
            type: 'vault-availability',
            id: 'vault',
            attributes: { enabled: true, instances: ['default'], 'default-instance': 'default' },
          },
        }),
      }),
    )

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-source').selectOption('vault')

    // Every coordinate field must be reachable and typable, not clipped.
    for (const id of ['#add-mount', '#add-path', '#add-field']) {
      await expect(page.locator(id)).toBeVisible()
    }
    await page.locator('#add-path').fill('apps/some/deeper/path')
    await expect(page.locator('#add-path')).toHaveValue('apps/some/deeper/path')
    await expectNoHorizontalPageScroll(page)
  })

  test('Vault file delivery is usable at phone width (#1619)', async ({ page }) => {
    // The toggle, the name box and its hint all have to hold up on a phone,
    // and a long file name in the list must wrap rather than push the page
    // sideways — the list is where an operator checks what a run will see.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erespvfile'), {
      'execution-mode': 'agent',
    })
    const longName = 'deeply/nested/directory/structure/for/a/service-account-credentials.json'
    const seed = await fetch(`${API_URL}/api/v2/workspaces/${wsId}/vars`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: {
          type: 'vars',
          attributes: {
            key: 'GOOGLE_APPLICATION_CREDENTIALS', category: 'env', 'value-source': 'vault',
            value: JSON.stringify({
              source: 'vault', mount: 'secret', path: 'apps/gcp', field: 'sa_json',
              file: { name: longName },
            }),
          },
        },
      }),
    })
    expect(seed.status).toBe(201)

    await page.route('**/api/terrapod/v1/vault/availability', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: {
            type: 'vault-availability',
            id: 'vault',
            attributes: { enabled: true, instances: ['default'], 'default-instance': 'default' },
          },
        }),
      }),
    )

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await expect(page.getByText(longName).filter({ visible: true }).first()).toBeVisible({ timeout: 10_000 })
    await expectNoHorizontalPageScroll(page)

    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-source').selectOption('vault')
    await page.locator('#add-file').check()
    await expect(page.locator('#add-file-name')).toBeVisible()
    await page.locator('#add-file-name').fill('~/.aws/credentials')
    await expect(page.locator('#add-file-name')).toHaveValue('~/.aws/credentials')
    await expect(page.getByText('/var/run/terrapod/files/', { exact: false })).toBeVisible()
    await expectNoHorizontalPageScroll(page)
  })

  test('Vault file templates and formats are usable at phone width (#1648)', async ({ page }) => {
    // A template is multi-line text: the textarea must fit the phone and let
    // a long line scroll inside itself, not push the page sideways. The
    // format's two controls stack rather than squeeze side by side.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erespvtpl'), {
      'execution-mode': 'agent',
    })
    await page.route('**/api/terrapod/v1/vault/availability', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: {
            type: 'vault-availability',
            id: 'vault',
            attributes: { enabled: true, instances: ['default'], 'default-instance': 'default' },
          },
        }),
      }),
    )

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).click()
    await page.locator('#var-source').selectOption('vault')
    await page.locator('#add-file').check()
    await page.locator('#add-file-content').selectOption('template')
    const longLine =
      'aws_secret_access_key = {{ secret_key }} # a deliberately long line that is much wider than a phone screen'
    await page.locator('#add-file-template').fill(`[default]\n${longLine}\n`)
    await expect(page.locator('#add-file-template')).toBeVisible()
    await expect(page.locator('#add-field')).toHaveCount(0)
    await expectNoHorizontalPageScroll(page)

    await page.locator('#add-file-content').selectOption('format')
    await expect(page.locator('#add-file-format')).toBeVisible()
    await expect(page.locator('#add-file-fields')).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    await page.locator('#add-file-content').selectOption('field')
    await expect(page.locator('#add-file-encoding')).toBeVisible()
    await expect(page.locator('#add-field')).toBeVisible()
    await expectNoHorizontalPageScroll(page)
  })

  test('workspace variable sets panel adapts to mobile (#1440)', async ({ page }) => {
    // Seeded rather than asserted on an empty page: with no set applying, the
    // panel renders nothing at all and the assertion would pass however the
    // layout is written.
    const token = getStoredToken()
    const wsName = uniqueName('e2erespvs')
    const wsId = await createWorkspace(token, wsName, { labels: { e2erespvs: wsName } })
    const res = await fetch(`${API_URL}/api/v2/organizations/default/varsets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: {
          attributes: {
            name: uniqueName('e2erespset'),
            'assignment-rule': { labels: { e2erespvs: wsName } },
          },
        },
      }),
    })
    expect(res.status).toBe(201)
    const vsName = (await res.json()).data.attributes.name

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    const entry = page.locator('li').filter({ hasText: vsName })
    await expect(entry).toBeVisible({ timeout: 15_000 })
    // The set name and its source badge both have to survive at phone width —
    // the source is the primary signal here, not decoration.
    await expect(entry.getByText('Matched by rule')).toBeVisible()
    await expectNoHorizontalPageScroll(page)
  })

  test('variable-set variables adapt to mobile (#1439)', async ({ page }) => {
    // Seeded with a real variable: on an empty set the page renders an empty
    // state and there is no table in the DOM at all, so the assertion would
    // pass however the breakpoints are written.
    const token = getStoredToken()
    const res = await fetch(`${API_URL}/api/v2/organizations/default/varsets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ data: { attributes: { name: uniqueName('e2erespvsv') } } }),
    })
    expect(res.status).toBe(201)
    const vsId = (await res.json()).data.id

    const varRes = await fetch(`${API_URL}/api/v2/varsets/${vsId}/relationships/vars`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: {
          type: 'vars',
          attributes: { key: 'RESP_KEY', category: 'env', value: 'resp-value' },
        },
      }),
    })
    expect(varRes.status).toBe(201)

    await page.goto(`/admin/variable-sets/${vsId}?tab=variables`)

    const card = page.locator('li').filter({ hasText: 'RESP_KEY' })
    await expect(card).toBeVisible({ timeout: 15_000 })
    // Nothing is dropped on the phone: the desktop table is the only place the
    // category column lives, so the card has to carry key, value AND category.
    await expect(card.getByText('resp-value')).toBeVisible()
    await expect(card.getByText('env')).toBeVisible()
    // The desktop table must not be the thing rendering at this width. It is
    // still in the DOM — `hidden md:block` hides it with CSS rather than
    // unmounting it — so this counts VISIBLE tables, not elements.
    await expect(page.locator('table:visible')).toHaveCount(0)
    await expectNoHorizontalPageScroll(page)
  })

  test('the variable-set Vault edit panel is usable at phone width (#1439)', async ({ page }) => {
    // The edit state used to be a set of table cells, which truncated the five
    // reference fields. Both breakpoints now share VariableEditPanel, so the
    // builder has to be reachable and typable from the mobile card.
    const token = getStoredToken()
    const res = await fetch(`${API_URL}/api/v2/organizations/default/varsets`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ data: { attributes: { name: uniqueName('e2erespvsedit') } } }),
    })
    expect(res.status).toBe(201)
    const vsId = (await res.json()).data.id

    const ref = JSON.stringify({ mount: 'secret', path: 'apps/thing', field: 'token' })
    const varRes = await fetch(`${API_URL}/api/v2/varsets/${vsId}/relationships/vars`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: {
          type: 'vars',
          attributes: { key: 'RESP_VAULT', category: 'env', 'value-source': 'vault', value: ref },
        },
      }),
    })
    expect(varRes.status).toBe(201)
    const varId = (await varRes.json()).data.id

    // The stack deliberately has no Vault, so the panel would not offer the
    // source without this.
    await page.route('**/api/terrapod/v1/vault/availability', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: {
            type: 'vault-availability',
            id: 'vault',
            attributes: { enabled: true, instances: ['default'], 'default-instance': 'default' },
          },
        }),
      }),
    )

    await page.goto(`/admin/variable-sets/${vsId}?tab=variables`)
    const card = page.locator('li').filter({ hasText: 'RESP_VAULT' })
    await expect(card).toBeVisible({ timeout: 15_000 })
    await card.getByRole('button', { name: 'Edit' }).click()

    // `medit-` is the mobile card's panel prefix; the desktop row uses `edit-`,
    // so this also proves the mobile branch is what rendered.
    for (const id of [`#medit-${varId}-mount`, `#medit-${varId}-path`, `#medit-${varId}-field`]) {
      await expect(page.locator(id)).toBeVisible()
    }
    await page.locator(`#medit-${varId}-path`).fill('apps/some/deeper/path')
    await expect(page.locator(`#medit-${varId}-path`)).toHaveValue('apps/some/deeper/path')
    await expectNoHorizontalPageScroll(page)
  })

  test('deleted-workspaces admin page adapts to mobile (#1253)', async ({ page }) => {
    // Seed a real deleted workspace first. Asserting the table is hidden on an
    // EMPTY page proves nothing — with no rows the component renders an empty
    // state and there is no table in the DOM at all, so the assertion passes
    // however the breakpoints are written.
    const token = getStoredToken()
    const name = uniqueName('e2eresp')
    const wsId = await createWorkspace(token, name)
    await fetch(`${API_URL}/api/terrapod/v1/workspaces/${wsId}`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${token}` },
    })

    await page.goto('/admin/deleted-workspaces')
    // Scoped to the card: the name appears in BOTH renders (the desktop table
    // is in the DOM, just hidden), so an unscoped getByText is ambiguous —
    // which is itself evidence the dual-render is present.
    const card = page.locator('ul > li').filter({ hasText: name })
    await expect(card).toBeVisible({ timeout: 10_000 })
    await expectNoHorizontalPageScroll(page)

    // Now the assertion bites: rows exist, so the desktop table is present in
    // the tree and must be hidden by width, with the card list rendering in
    // its place. One component driven by the breakpoint, not a forked build.
    await expect(page.locator('table')).toBeHidden()
  })

  test('VCS connection consumption renders at phone width (#1339)', async ({ page }) => {
    // Seed a real connection first. On an empty page the table is not in the
    // DOM at all, so the assertion would pass however the breakpoints are
    // written — the same trap as the deleted-workspaces test above.
    const token = getStoredToken()
    const name = uniqueName('e2eresp-vcs')
    const created = await fetch(`${API_URL}/api/terrapod/v1/vcs-connections`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/vnd.api+json' },
      body: JSON.stringify({
        data: { type: 'vcs-connections', attributes: { name, provider: 'gitlab', token: 'glpat-e2e-not-a-real-token' } },
      }),
    })
    expect(created.ok).toBeTruthy()
    const connId = (await created.json()).data.id

    try {
      await page.goto('/admin/vcs-connections')
      await expect(page.getByText(name)).toBeVisible({ timeout: 15_000 })

      // A freshly created connection has made no calls and the server has
      // reported no budget, so there is nothing to classify: it reads **Not
      // reported**.
      //
      // This assertion previously expected "Idle", which is what the code did
      // and exactly the defect (#1345): a verdict was fabricated from the
      // absence of any reading, so a GitLab connection being polled hard —
      // GitLab was not instrumented at all — rendered a calm grey badge while
      // the runbook sent the operator here to diagnose stalled runs. A verdict
      // is now withheld unless there is a budget to judge against; the rate is
      // still reported, because that is Terrapod's own tally.
      await expect(page.getByText('Not reported').first()).toBeVisible({ timeout: 15_000 })

      // The connection renders as a panel (#1339) rather than a table row, so
      // it reflows to a single column here — nothing may push the page sideways.
      await expectNoHorizontalPageScroll(page)
    } finally {
      await fetch(`${API_URL}/api/terrapod/v1/vcs-connections/${connId}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${token}` },
      })
    }
  })

  test('both queue buttons are reachable at phone width (#1340)', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2eresp-queue'))

    await page.goto(`/workspaces/${wsId}?tab=runs`)

    // Plan-vs-apply is the decision that matters every time, so neither button
    // may be hidden behind a breakpoint — a phone must be able to make the
    // same choice a desktop can.
    await expect(page.getByRole('button', { name: 'Plan', exact: true })).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByRole('button', { name: 'Plan + apply', exact: true })).toBeVisible()

    // Four buttons on one row is where a phone starts scrolling sideways.
    await expectNoHorizontalPageScroll(page)
  })

  test('a long lock reason wraps inside the lock card at phone width (#1705)', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2eresp-lockreason'))
    // One unbroken token is the case that pushes a card sideways.
    await lockWorkspace(token, wsId, `change-freeze-${'x'.repeat(120)} until the release is out`)

    await page.goto(`/workspaces/${wsId}`)

    await expect(page.getByTestId('lock-reason')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('lock-holder')).toBeVisible()
    // The Unlock action stays reachable beside a long reason.
    await expect(page.getByRole('button', { name: 'Unlock', exact: true })).toBeVisible()
    await expectNoHorizontalPageScroll(page)
  })

  test('runs at a phone viewport', async ({ page }) => {
    const vp = page.viewportSize();
    expect(vp, 'responsive project must set a viewport').not.toBeNull();
    expect(vp!.width, 'responsive project runs below the md breakpoint').toBeLessThan(768);
  });

  test('nav adapts to mobile: hamburger shown, grouped sheet', async ({ page }) => {
    await page.goto('/workspaces');
    await expectNoHorizontalPageScroll(page);

    // The mobile branch of the nav renders a hamburger toggle; the desktop
    // link row is hidden below md. Proves the single nav component adapts
    // by width — no forked mobile build (#719).
    const hamburger = page.getByRole('button', { name: /open menu/i });
    await expect(hamburger).toBeVisible();

    // Opening it reveals the grouped sheet: primary links plus labelled
    // sections (Registry / Help, + Admin for admins). Account is NOT here — it
    // has its own trigger + drawer.
    //
    // The nav is a client component: right after navigation the hamburger is
    // SSR-rendered and visible, but a click can land BEFORE React hydrates and
    // wires its onClick — the click is swallowed, `menuOpen` never flips, and
    // #mobile-nav-menu never mounts (the pre-hydration lost-click flake, #902).
    // Retry the click until the sheet actually opens, clicking only while it's
    // still closed so a late-hydrated handler can't toggle it back shut.
    const menu = page.locator('#mobile-nav-menu');
    await expect(async () => {
      if (!(await menu.isVisible())) await hamburger.click();
      await expect(menu).toBeVisible({ timeout: 1000 });
    }).toPass({ timeout: 15000 });
    // exact: the Admin group also carries a "Deleted workspaces" link (#1253),
    // which a substring match would pick up as a second element.
    await expect(menu.getByRole('link', { name: 'Workspaces', exact: true })).toBeVisible();
    await expect(menu.getByText('Registry', { exact: true })).toBeVisible();
    await expect(menu.getByText('Help', { exact: true })).toBeVisible();
    await expect(menu.getByRole('link', { name: 'Modules' })).toBeVisible();
    await expect(menu.getByText('Account', { exact: true })).toHaveCount(0);
    // Opening the sheet must not introduce horizontal overflow.
    await expectNoHorizontalPageScroll(page);

    // Account has its own trigger + drawer (personal/session items + log out).
    await menu.getByRole('button', { name: /close menu/i }).click();
    // Wait for the nav sheet to fully close before opening the account drawer.
    // The open sheet is a full-screen `fixed … z-40` overlay covering the account
    // trigger in the top bar; closing it unmounts the drawer via a React state
    // update. Driving the account-open click before that unmount completes lets
    // the click miss (the overlay still intercepts) so the account drawer never
    // opens — the intermittent "element(s) not found" flake (#896). Gate on the
    // sheet being gone, then on the trigger being actionable, before clicking.
    await expect(menu).toBeHidden();
    const accountTrigger = page.getByRole('button', { name: 'Open account menu' });
    await expect(accountTrigger).toBeVisible();
    await accountTrigger.click();
    const account = page.locator('#mobile-account-menu');
    await expect(account).toBeVisible();
    await expect(account.getByRole('link', { name: 'API Tokens' })).toBeVisible();
    await expect(account.getByRole('button', { name: 'Log out' })).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });

  test('workspace list surfaces status in-row at phone width', async ({ page }) => {
    // Below `lg` the STATUS table column is hidden, so the row must carry an
    // inline status indicator — otherwise a phone loses the running/errored/
    // applied signal entirely (regression the mobile status line fixes, #719).
    const token = getStoredToken();
    const name = uniqueName('resp-status');
    await createWorkspace(token, name);

    // The client-side filter reads the `q` query param — narrow to our row.
    await page.goto(`/workspaces?q=${encodeURIComponent(name)}`);
    const row = page.getByRole('row').filter({ hasText: name });
    await expect(row).toBeVisible();

    // The inline mobile status indicator is present (a fresh workspace shows
    // "—", a run-bearing one shows its coloured pill — either way, not hidden).
    await expect(row.getByTestId('ws-row-status-mobile')).toBeVisible();

    // The dedicated desktop STATUS column header stays hidden at this width.
    await expect(page.getByRole('columnheader', { name: 'Status' })).toBeHidden();

    await expectNoHorizontalPageScroll(page);
  });

  test('workspace list trims secondary chrome at phone width', async ({ page }) => {
    // On a phone we drop the explanatory subtitle and the Total/Locked
    // stat cards (secondary), but KEEP Health Issues (the primary signal
    // that something needs attention) — #719.
    await page.goto('/workspaces');
    await expect(page.getByRole('heading', { name: 'Workspaces', level: 1 })).toBeVisible();

    await expect(page.getByText('Manage Terraform workspaces, state, and runs')).toBeHidden();
    // Compact stat chips: Total/Locked are desktop-only; Health always shows.
    await expect(page.getByText('Total', { exact: true })).toBeHidden();
    await expect(page.getByText('Locked', { exact: true })).toBeHidden();
    await expect(page.getByText('Health', { exact: true })).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });

  test('grouped workspace tree stays within the phone at repo-path depth', async ({ page }) => {
    // Grouping adds an interactive tree to the list (Group by repo / repo-path).
    // A deep repo-path tree is the shape that pushes a table sideways on a
    // phone — nested indentation plus a full table row — so it needs pinning
    // that it does not introduce a horizontal page scroll, and that the
    // per-row mobile status badge (the only status signal below `lg`) still
    // renders inside a grouped row. The `?group=` URL param is the source of
    // truth for the mode (localStorage is only a fallback), so a deep link
    // opens straight into the grouped view without any interaction.
    const token = getStoredToken();
    // Unique repo basename + a shared name token: the repo makes this test's
    // group distinct from any parallel spec's, and the `q=` name filter narrows
    // the list to just these two rows so no other workspace bleeds into the
    // tree (E2E isolation — never assert against the shared unfiltered list).
    const slug = uniqueName('respgrp').replace(/[^a-z0-9]/gi, '').toLowerCase();
    const repo = `https://github.com/org/${slug}.git`;
    const wsRoot = `${slug}-root`;
    const wsDeep = `${slug}-deep`;
    await createWorkspace(token, wsRoot, { 'vcs-repo-url': repo });
    await createWorkspace(token, wsDeep, {
      'vcs-repo-url': repo,
      'working-directory': 'environments/prod/us-east-1',
    });

    await page.goto(`/workspaces?q=${encodeURIComponent(slug)}&group=repo-path`);

    // The mode came from the URL (not a stored default), and it survives.
    await expect(page).toHaveURL(/[?&]group=repo-path/);

    // The repo group header renders, labelled by the unique repo basename;
    // below it the nested path segments are their own collapsible group rows.
    // Match the header by its "<label>/" text (the trailing slash the tree
    // renders) so the locator does not also catch the active filter pill,
    // whose accessible name is "name: <slug>" (no trailing slash).
    await expect(
      page.getByRole('button', { name: new RegExp(`^${slug}/`) }),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: /^environments\// })).toBeVisible();

    // The deep-nested workspace is reachable and carries its inline mobile
    // status indicator inside the grouped row (preserved under grouping).
    const deepRow = page.getByRole('row').filter({ hasText: wsDeep });
    await expect(deepRow).toBeVisible();
    await expect(deepRow.getByTestId('ws-row-status-mobile')).toBeVisible();

    // The whole point: a deep tree must not push the page sideways on a phone.
    await expectNoHorizontalPageScroll(page);
  });

  test('the plan-log index is usable at phone width (#1590)', async ({ page }) => {
    // A native <select> is the right control on touch: the platform renders it
    // as its own picker, so a long list of resource addresses never needs a
    // custom menu that could overflow the viewport. The index only exists once
    // the log is whole, so the stub ends the body with ETX.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('resp-idx'));
    const runId = await seedRun(token, wsId);
    const body =
      '\x02Terraform will perform the following actions:\n\n' +
      '  # aws_instance.web will be updated in-place\n' +
      '  ~ resource "aws_instance" "web" {\n    }\n\n' +
      '  # aws_s3_bucket.assets will be created\n' +
      '  + resource "aws_s3_bucket" "assets" {\n    }\n\n' +
      'Plan: 1 to add, 1 to change, 0 to destroy.\n\x03';

    await page.route(`**/api/v2/runs/${runId}`, async (route: Route) => {
      const res = await route.fetch();
      const json = await res.json();
      json.data.attributes.status = 'planned';
      await route.fulfill({ response: res, body: JSON.stringify(json) });
    });
    await page.route(`**/api/terrapod/v1/runs/${runId}/plan`, (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({
          data: { id: 'p', type: 'plans', attributes: { 'log-read-url': '/__e2e_idx_log' } },
        }),
      }));
    await page.route('**/__e2e_idx_log*', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/plain',
        body: new URL(route.request().url()).searchParams.get('offset') === '0' ? body : '',
      }));

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=plan`);

    const index = page.getByTestId('log-index-plan');
    await expect(index).toBeVisible();
    // A real tap target, not a sliver of text.
    const box = await index.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(24);
    // The whole point: a long resource address must not widen the page.
    await expectNoHorizontalPageScroll(page);
  });

  test('the run log pane reserves no height on touch (#1547, #722)', async ({ page }) => {
    // #1547 reserves the pane's full height while a phase streams — with a
    // precise pointer only. On touch the page is the scroll container (#722):
    // no fixed-height box, no inner scrollbar. This project is a Pixel 7, so
    // `pointer: coarse`, which is exactly the case the `fine:` gate must exclude.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('resp-log'));
    const runId = await seedRun(token, wsId);
    const body = Array.from({ length: 40 }, (_, i) => `plan ${i}  Still reading...`).join('\n') + '\n';

    await page.route(`**/api/v2/runs/${runId}`, async (route: Route) => {
      const res = await route.fetch();
      const json = await res.json();
      json.data.attributes.status = 'planning';
      await route.fulfill({ response: res, body: JSON.stringify(json) });
    });
    await page.route(`**/api/terrapod/v1/runs/${runId}/plan`, (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({
          data: { id: 'p', type: 'plans', attributes: { 'log-read-url': '/__e2e_resp_log', status: 'running' } },
        }),
      }));
    await page.route('**/__e2e_resp_log*', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/plain',
        body: new URL(route.request().url()).searchParams.get('offset') === '0' ? body : '',
      }));

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=plan`);
    const pre = page.getByTestId('log-pre-plan');
    await expect(pre).toBeVisible({ timeout: 20_000 });

    const reserved = await page
      .getByTestId('log-reserve-plan')
      .evaluate((el) => getComputedStyle(el).minHeight);
    expect(reserved).toBe('0px');
    const { overflowY, maxHeight } = await pre.evaluate((el) => {
      const s = getComputedStyle(el);
      return { overflowY: s.overflowY, maxHeight: s.maxHeight };
    });
    expect(overflowY).toBe('visible');
    expect(maxHeight).toBe('none');
    await expectNoHorizontalPageScroll(page);
  });

  test('run detail page: native view picker drives navigation at phone width', async ({ page }) => {
    // The run-detail page is the hard mobile surface (#721/#722): the view
    // tabs collapse to a native <select> (no horizontal-scroll strip), the URL
    // stays the source of truth for the active view, and there is no horizontal
    // page scroll. Seed a run — the E2E stack has no runner so it sits `queued`,
    // which renders the whole page without needing real execution.
    const token = getStoredToken();
    const wsName = uniqueName('resp-run');
    const wsId = await createWorkspace(token, wsName);
    const runId = await seedRun(token, wsId);

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=overview`);
    await expect(
      page.getByRole('heading', { name: new RegExp(wsName), level: 1 }),
    ).toBeVisible({ timeout: 15_000 });

    // Below md the tabs are a native <select>, not a scrolling tab strip.
    const picker = page.locator('#run-view-select');
    await expect(picker).toBeVisible();
    await expectNoHorizontalPageScroll(page);

    // The picker is the source of truth for the active view — selecting an
    // option updates the URL (survives reload / back / deep-link).
    await picker.selectOption('plan');
    await expect(page).toHaveURL(/[?&]view=plan/);
    await expectNoHorizontalPageScroll(page);
  });

  test('workspace runs list becomes tappable cards at phone width', async ({ page }) => {
    // The 7-column runs table is unreadable on a phone, so below md it renders
    // as stacked cards driven by the same data (#719 Stage 2). The desktop
    // table header is hidden; the seeded run shows as a card that is itself a
    // link to the run (one big tap target).
    const token = getStoredToken();
    const wsName = uniqueName('resp-runs');
    const wsId = await createWorkspace(token, wsName);
    await seedRun(token, wsId);

    await page.goto(`/workspaces/${wsId}?tab=runs`);

    // The 9-tab strip collapses to a native <select> section picker at phone
    // width (the tab bar overflows a phone), driven by the same ?tab= URL.
    await expect(page.locator('#ws-tab-select')).toBeVisible();
    // The desktop table's column header is hidden at phone width...
    await expect(page.getByRole('columnheader', { name: 'Run ID' })).toBeHidden();
    // ...and the run renders as a card linking to the run detail page.
    await expect(page.locator('a[href*="/runs/run-"]').first()).toBeVisible({ timeout: 15_000 });

    await expectNoHorizontalPageScroll(page);
  });

  test('workspace cost tab has no horizontal page scroll at phone width', async ({ page }) => {
    // The Cost tab (#871) — headline card + per-resource table (which collapses
    // to stacked cards below sm). A fresh workspace has no state, so it shows
    // the deterministic empty state; either way the page must not h-scroll.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('resp-cost'));
    await page.goto(`/workspaces/${wsId}?tab=cost`);
    await expect(page.getByText(/No cost yet/i)).toBeVisible({ timeout: 15_000 });
    await expectNoHorizontalPageScroll(page);
  });

  test('workspace state list becomes cards at phone width', async ({ page }) => {
    // The state-version table hid Created-by / Run / Size / Created behind
    // sm/md/lg breakpoints, leaving a phone with only the serial. Below md it
    // renders as cards driven by the same data (#719), so nothing is dropped.
    const token = getStoredToken();
    const wsName = uniqueName('resp-state');
    const wsId = await createWorkspace(token, wsName);
    await seedStateVersion(token, wsId, 1);

    await page.goto(`/workspaces/${wsId}?tab=state`);

    // The 9-tab strip is the native <select> picker at phone width.
    await expect(page.locator('#ws-tab-select')).toBeVisible();
    // The desktop table's Serial column header is hidden below md...
    await expect(page.getByRole('columnheader', { name: 'Serial' })).toBeHidden();
    // ...and the state version renders as a card with its serial and a Download
    // button. `#1` + Download also exist in the hidden desktop table, so filter
    // to the visible (mobile-card) copy.
    await expect(page.getByText('#1', { exact: true }).filter({ visible: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: 'Download' }).filter({ visible: true })).toBeVisible();

    await expectNoHorizontalPageScroll(page);
  });

  test('workspace state graph defaults to the accessible table at phone width', async ({ page }) => {
    // The state resource graph (#765) is WebGL-heavy and desktop-oriented, so a
    // phone defaults to the accessible table (via useIsMobile) — never a blank
    // canvas — and must not scroll horizontally.
    const token = getStoredToken();
    const wsName = uniqueName('resp-stategraph');
    const wsId = await createWorkspace(token, wsName);
    await seedStateVersionWithContent(token, wsId, [
      { mode: 'managed', type: 'null_resource', name: 'hub', instances: [{ dependencies: [] }] },
    ]);

    await page.goto(`/workspaces/${wsId}?tab=state-graph`);

    // Phone → Table view is the default: the resource is listed as a rowheader.
    await expect(page.getByRole('rowheader', { name: 'null_resource.hub' })).toBeVisible({ timeout: 15_000 });
    await expectNoHorizontalPageScroll(page);
  });

  test('workspace configurations list becomes cards at phone width', async ({ page }) => {
    // The 6-column configuration-versions table is 529px wide and was clipped
    // by its overflow-hidden wrapper on a phone (Created + Download vanished).
    // Below md it renders as cards driven by the same data (#719). Seeding a run
    // uploads a configuration version.
    const token = getStoredToken();
    const wsName = uniqueName('resp-cfg');
    const wsId = await createWorkspace(token, wsName);
    await seedRun(token, wsId);

    await page.goto(`/workspaces/${wsId}?tab=versions`);

    await expect(page.locator('#ws-tab-select')).toBeVisible();
    // The desktop table's column header is hidden at phone width...
    await expect(page.getByRole('columnheader', { name: 'Source' })).toBeHidden();
    // ...and the config version renders as a card exposing its full id + the
    // Compare checkbox. Both also exist in the hidden desktop table, so filter
    // to the visible (mobile-card) copy.
    await expect(page.getByText(/^cv-/).filter({ visible: true }).first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('checkbox', { name: /Select cv-.* for compare/ }).filter({ visible: true }).first()).toBeVisible();

    await expectNoHorizontalPageScroll(page);
  });

  test('admin users page fits a phone + delete/toggle are two-tier confirm buttons', async ({ page }) => {
    // Representative deep-admin surface (#719): the users table hides secondary
    // columns below breakpoints but keeps the Active/Inactive status in-row; the
    // row actions are real buttons; delete confirms in BOTH modes and the
    // activate/deactivate toggle confirms on touch (this Pixel project).
    const token = getStoredToken();
    const email = `${uniqueName('resp-user')}@example.com`;
    await createUser(token, email, 'Sup3rSecret!pw', 'Resp User');

    await page.goto('/admin/users');
    const row = page.getByRole('row').filter({ hasText: email });
    await expect(row).toBeVisible({ timeout: 15_000 });
    // Status stays visible at phone width (not hidden behind a breakpoint).
    await expect(row.getByRole('button', { name: /Active|Inactive/ })).toBeVisible();
    // Row actions are real buttons (Delete present as a button, not bare text).
    await expect(row.getByRole('button', { name: 'Delete' })).toBeVisible();
    await expectNoHorizontalPageScroll(page);

    // Tier-1 delete prompts on touch (and would on desktop too); dismiss keeps the row.
    let deleteMsg = '';
    page.once('dialog', async (d) => { deleteMsg = d.message(); await d.dismiss(); });
    await row.getByRole('button', { name: 'Delete' }).click();
    await expect.poll(() => deleteMsg, { timeout: 5_000 }).toContain('Delete user');
    await expect(row).toBeVisible();

    // Tier-2 activate/deactivate toggle prompts on touch; dismiss keeps state.
    let toggleMsg = '';
    page.once('dialog', async (d) => { toggleMsg = d.message(); await d.dismiss(); });
    await row.getByRole('button', { name: /Active|Inactive/ }).click();
    await expect.poll(() => toggleMsg, { timeout: 5_000 }).toMatch(/Deactivate|Activate/);
  });

  test('agent pools list + detail fit a phone viewport', async ({ page }) => {
    // Agent Pools is a top-level admin surface (#719). The list hides the
    // STATUS column below md, so the pool's health dot must reflow inline into
    // the row; the detail page (settings + tokens + listeners tables) must not
    // introduce horizontal page scroll.
    const token = getStoredToken();
    const poolName = uniqueName('resp-pool');
    const poolId = await createAgentPool(token, poolName);

    await page.goto('/admin/agent-pools');
    const row = page.getByRole('row').filter({ hasText: poolName });
    await expect(row).toBeVisible({ timeout: 15_000 });
    // The dedicated desktop STATUS column header stays hidden at phone width.
    await expect(page.getByRole('columnheader', { name: 'Status' })).toBeHidden();
    await expectNoHorizontalPageScroll(page);

    await page.goto(`/admin/agent-pools/${poolId}`);
    await expect(
      page.getByRole('heading', { name: new RegExp(poolName), level: 1 }),
    ).toBeVisible({ timeout: 15_000 });
    await expectNoHorizontalPageScroll(page);
  });

  test('touch: both a reversible toggle and an irreversible delete prompt confirm()', async ({ page }) => {
    // #719 two-tier confirm policy, coarse-pointer half. On touch EVERY mutation
    // prompts: tier-2 (toggle) — which on a precise pointer would NOT — and
    // tier-1 (delete). This Pixel project is the only proof of the touch path,
    // since the maintainer doesn't test on a real device.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('confirm-touch'));
    const rtName = uniqueName('rt');
    await seedRunTask(token, wsId, rtName);

    await page.goto(`/workspaces/${wsId}?tab=run-tasks`);
    await expect(page.getByText(rtName)).toBeVisible({ timeout: 15_000 });

    // Handlers registered BEFORE the click: window.confirm() is synchronous and
    // blocks the click handler, so the dialog must be handled as it opens
    // (waitForEvent + click deadlocks).

    // Tier 2 — the Disable toggle DOES prompt on touch; dismiss keeps it enabled.
    let toggleMsg = '';
    page.once('dialog', async (d) => { toggleMsg = d.message(); await d.dismiss(); });
    await page.getByRole('button', { name: 'Disable' }).click();
    await expect.poll(() => toggleMsg, { timeout: 5_000 }).toContain('Disable this run task');
    await expect(page.getByText('Enabled', { exact: true })).toBeVisible();

    // Tier 1 — delete prompts on touch too; dismiss keeps the row.
    let deleteMsg = '';
    page.once('dialog', async (d) => { deleteMsg = d.message(); await d.dismiss(); });
    await page.getByRole('button', { name: 'Delete' }).click();
    await expect.poll(() => deleteMsg, { timeout: 5_000 }).toContain('Delete run task');
    await expect(page.getByText(rtName)).toBeVisible();
  });

  test('catalog browse page renders without horizontal scroll at phone width', async ({ page }) => {
    // The catalog browse page is a responsive card grid (or an empty state);
    // either way it must not scroll horizontally on a phone.
    await page.goto('/catalog');
    await expect(page.getByRole('heading', { name: 'Service Catalog' })).toBeVisible({ timeout: 15_000 });
    await expectNoHorizontalPageScroll(page);
  });

  test('a catalog item page fits a phone: provision form, interface and instances', async ({ page }) => {
    // The item, its form, interface and instances are stubbed so the page has
    // every section populated — including an instances table, which must
    // scroll inside its own container rather than widen the page.
    const itemId = 'cat-0198e2e0-0000-7000-8000-00000000c001';
    const base = `/api/terrapod/v1/catalog-items/${itemId}`;
    const meta = { pagination: { 'current-page': 1, 'page-size': 1, 'total-count': 1, 'total-pages': 1 } };
    const stubs: Record<string, unknown> = {
      [base]: {
        data: {
          id: itemId,
          type: 'catalog-items',
          attributes: {
            name: 'e2e-network',
            'display-name': 'A network with a rather long display name to wrap on a phone',
            description: 'Provisions a network with subnets, route tables and a NAT gateway per zone.',
            enabled: true,
            'module-id': 'mod-e2e',
            'module-name': 'network-with-a-long-module-name',
            'module-provider': 'aws',
            'default-version-pin': '1.2.3',
            'allowed-agent-pool-ids': null,
          },
        },
      },
      [`${base}/form`]: {
        data: {
          type: 'catalog-item-forms',
          attributes: {
            'resolved-version': '1.2.3',
            fields: [
              { name: 'cidr_block_for_the_primary_network', type: 'string', description: 'The CIDR block.', required: true, sensitive: false, default: '10.0.0.0/16', options: null, source: 'module' },
              { name: 'environment', type: 'string', description: '', required: false, sensitive: false, default: 'dev', options: ['dev', 'staging', 'prod'], source: 'catalog' },
            ],
          },
        },
      },
      [`${base}/interface`]: {
        data: {
          type: 'catalog-item-interfaces',
          attributes: {
            'resolved-version': '1.2.3',
            inputs: [{ name: 'cidr_block_for_the_primary_network', type: 'string', description: 'The CIDR block.', default: null, required: true, sensitive: false }],
            outputs: [{ name: 'vpc_id', description: 'The network id.', sensitive: false }],
          },
        },
      },
      [`${base}/instances`]: {
        data: [
          {
            id: 'ws-0198e2e0-0000-7000-8000-00000000c002',
            type: 'workspaces',
            attributes: {
              name: 'e2e-network-instance-with-a-long-name',
              'catalog-item-id': itemId,
              'catalog-version-pin': '1.2.3',
              'agent-pool-id': null,
              'owner-email': 'admin@example.com',
              labels: {},
            },
          },
        ],
        meta,
      },
    };
    await page.route(
      (url) => url.pathname in stubs,
      (route) => route.fulfill({ json: stubs[new URL(route.request().url()).pathname] }),
    );

    await page.goto(`/catalog/${itemId}`);
    await expect(
      page.getByRole('heading', { name: 'A network with a rather long display name to wrap on a phone' }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('#prov-name')).toBeVisible();
    await expect(page.getByText('e2e-network-instance-with-a-long-name')).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });

  test('a catalog item whose module could not be parsed shows why, and still fits a phone (#1707)', async ({
    page,
  }) => {
    // The form comes back with no fields because the module failed to parse;
    // the warning carrying the reason must be visible, and a long reason must
    // wrap rather than widen the page.
    const itemId = 'cat-0198e2e0-0000-7000-8000-00000000c707';
    const base = `/api/terrapod/v1/catalog-items/${itemId}`;
    const reason =
      'variables_with_a_rather_long_file_name_for_a_phone.tf: invalid HCL at line 12, column 7; main.tf: invalid HCL at line 3, column 1';
    const stubs: Record<string, unknown> = {
      [base]: {
        data: {
          id: itemId,
          type: 'catalog-items',
          attributes: {
            name: 'e2e-broken',
            'display-name': 'A broken module',
            description: '',
            enabled: true,
            'module-id': 'mod-e2e',
            'module-name': 'broken',
            'module-provider': 'aws',
            'default-version-pin': null,
            'allowed-agent-pool-ids': null,
          },
        },
      },
      [`${base}/form`]: {
        data: {
          type: 'catalog-item-forms',
          attributes: { 'resolved-version': '1.0.0', fields: [], 'interface-error': reason },
        },
      },
      [`${base}/interface`]: {
        data: {
          type: 'catalog-item-interfaces',
          attributes: { 'resolved-version': '1.0.0', inputs: [], outputs: [], 'interface-error': reason },
        },
      },
      [`${base}/instances`]: {
        data: [],
        meta: { pagination: { 'current-page': 1, 'page-size': 0, 'total-count': 0, 'total-pages': 0 } },
      },
    };
    await page.route(
      (url) => url.pathname in stubs,
      (route) => route.fulfill({ json: stubs[new URL(route.request().url()).pathname] }),
    );

    await page.goto(`/catalog/${itemId}`);
    const warning = page.getByTestId('module-interface-error').first();
    await expect(warning).toBeVisible({ timeout: 15_000 });
    await expect(warning).toContainText(reason);
    await expect(warning).toContainText('This form may be missing variables the module needs.');
    await expectNoHorizontalPageScroll(page);
  });

  test('registry module list renders as a card grid at phone width', async ({ page }) => {
    // The registry list pages are responsive card grids (grid-cols-1 at phone),
    // so a seeded module shows as a full-width card with no horizontal scroll.
    const token = getStoredToken();
    const modName = uniqueName('respmod').replace(/[^a-z0-9]/gi, '');
    await createRegistryModule(token, modName, 'aws');

    await page.goto('/registry/modules');
    await expect(page.getByText(modName).first()).toBeVisible({ timeout: 15_000 });

    await expectNoHorizontalPageScroll(page);
  });

  test('a repository group of modules does not scroll sideways at phone width (#1583)', async ({
    page,
  }) => {
    // Modules sharing a repository are headed by its URL, which can be long;
    // it has to wrap rather than push the page wider than the phone.
    const token = getStoredToken();
    const stamp = Date.now().toString(36);
    const repo = `https://github.com/e2e-org/a-rather-long-repository-name-to-wrap-${stamp}`;
    await createRegistryModule(token, `respmgroot${stamp}`, 'aws', { 'vcs-repo-url': repo });
    await createRegistryModule(token, `respmgsub${stamp}`, 'aws', {
      'vcs-repo-url': repo,
      subdirectory: 'modules/create',
    });

    await page.goto('/registry/modules');
    await expect(page.getByRole('heading', { name: repo })).toBeVisible({ timeout: 15_000 });

    await expectNoHorizontalPageScroll(page);
  });

  test('the module discovery panel fits a phone, with real checkboxes (#1584)', async ({ page }) => {
    // The scan is stubbed (the e2e stack has no repository to read). The panel's
    // inputs stack to one column, the long path wraps, and each candidate's
    // checkbox label is a full-width tap target.
    await page.route(
      (url) => url.pathname === '/api/terrapod/v1/vcs-connections',
      (route) =>
        route.fulfill({
          json: {
            data: [
              { id: 'vcs-e2e', type: 'vcs-connections', attributes: { name: 'e2e-github', provider: 'github' } },
            ],
            meta: { pagination: { 'current-page': 1, 'page-size': 1, 'total-count': 1, 'total-pages': 1 } },
          },
        }),
    );
    await page.route(
      (url) => url.pathname === '/api/terrapod/v1/module-autodiscovery-rules/preview',
      (route) =>
        route.fulfill({
          json: {
            data: {
              type: 'module-autodiscovery-rule-previews',
              attributes: {
                ref: 'main',
                'files-walked': 2,
                entries: [
                  {
                    subdirectory: 'modules/a-rather-long-directory-name/that-has-to-wrap-on-a-phone',
                    name: 'network-that-has-to-wrap-on-a-phone',
                    provider: 'aws',
                    'registered-as': null,
                    collision: false,
                    'missing-provider': false,
                  },
                ],
              },
            },
          },
        }),
    );

    await page.goto('/registry/modules');
    const panel = page.getByRole('heading', { name: 'Discover modules in a repository' });
    await expect(async () => {
      if (!(await panel.isVisible())) await page.getByRole('button', { name: 'Discover modules' }).click();
      await expect(panel).toBeVisible({ timeout: 1_000 });
    }).toPass({ timeout: 15_000 });
    await page.getByLabel('VCS connection').selectOption('vcs-e2e');
    await page.getByLabel('Repository URL').fill('https://github.com/e2e-org/terraform-aws-network');
    await page.getByRole('button', { name: 'Scan repository' }).click();

    const box = page.getByRole('checkbox', {
      name: 'Register the module in modules/a-rather-long-directory-name/that-has-to-wrap-on-a-phone',
    });
    await expect(box).toBeVisible();
    await box.check();
    await expect(page.getByRole('button', { name: 'Register 1 module' })).toBeEnabled();

    await expectNoHorizontalPageScroll(page);
  });

  test('estate topology defaults to the table at phone width (#763)', async ({ page }) => {
    // On a phone the estate page defaults to the accessible Table view rather
    // than heavy WebGL (#736 a11y + #719 mobile). Assert the table renders and
    // the page does not scroll horizontally.
    const token = getStoredToken()
    const wsName = uniqueName('e2e-estate-mob')
    await createWorkspace(token, wsName, { labels: { team: 'estate-mob' } })

    await page.goto('/estate')
    await expect(page.getByRole('heading', { name: 'Estate topology', level: 1 })).toBeVisible({
      timeout: 15_000,
    })
    // Phone default is the table — its Workspaces heading is visible without a toggle.
    await expect(page.getByRole('columnheader', { name: 'Workspace' })).toBeVisible({
      timeout: 10_000,
    })
    await expectNoHorizontalPageScroll(page)
  })

  test('impact graph is gated in the mobile view picker (#761)', async ({ page }) => {
    // The Impact graph (#761) lives on the run-detail page; per #719 the gating
    // must hold on mobile too. The E2E stack has no runner, so a seeded run has no
    // plan JSON output — the WebGL graph is unreachable (verified on the live Tilt
    // stack; its overlay panels are viewport-capped). Here we guard the mobile
    // surface: the run page fits the phone viewport and the native view picker
    // offers NO Impact option without plan JSON output.
    const token = getStoredToken();
    const wsName = uniqueName('e2e-impact-mob');
    const wsId = await createWorkspace(token, wsName);
    const runId = await seedRun(token, wsId, true);

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=overview`);
    const picker = page.locator('#run-view-select');
    await expect(picker).toBeVisible({ timeout: 15_000 });
    await expect(picker.locator('option[value="impact"]')).toHaveCount(0);
    await expectNoHorizontalPageScroll(page);
  });
});

/**
 * Tablet-width guard for the md–lg dead-zone (#839).
 *
 * The phone `responsive` project runs below md and the desktop projects run
 * well above lg, so the 768–1023px band — where the desktop nav used to render
 * (at md) but not fit until lg, wrapping into a tall sticky bar that shoved page
 * content (incl. the run/workspace tab bar) out of view — was tested by NEITHER
 * side. This block pins that band: the nav must still be its compact hamburger
 * form (desktop link row is `hidden lg:flex`, so it must NOT show here), and no
 * page must scroll horizontally — including the ≤11-tab workspace-detail strip,
 * which now scrolls within itself (`overflow-x-auto`) instead of overflowing the
 * page. One DRY viewport-driven UI, verified at the seam.
 */
test.describe('Tablet width (md–lg dead-zone, #839)', () => {
  test.use({ viewport: { width: 900, height: 900 }, isMobile: false });

  test('nav shows the icon bar, not the hamburger, no page h-scroll', async ({ page }) => {
    await page.goto('/workspaces');
    // #839 resolved this width by showing the hamburger; #1400 reversed that.
    // The icon-only bar needs about 650px, so hiding it here hid a bar that
    // fits. The nav now switches at md, where the rest of the UI switches to
    // its phone treatment, so 900px is squarely desktop and gets the bar.
    await expect(page.getByRole('button', { name: /open menu/i })).toBeHidden();
    await expect(
      page.getByRole('navigation').getByRole('link', { name: 'Workspaces', exact: true }),
    ).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });

  test('workspace-detail tab strip scrolls within itself, no page h-scroll', async ({ page }) => {
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('e2e-tabstrip-tablet'));

    await page.goto(`/workspaces/${wsId}`);
    // At ≥md the desktop tab bar renders (not the mobile <select>).
    await expect(page.getByRole('button', { name: 'Configuration' })).toBeVisible({ timeout: 15_000 });
    // The ~11-tab strip must not push the page into horizontal scroll — it is
    // contained by overflow-x-auto on its wrapper (the #839 fix).
    await expectNoHorizontalPageScroll(page);
  });

})

test.describe('Role reach panel (#1456)', () => {
  test('the reverse access view fits a phone without scrolling the page sideways', async ({ page }) => {
    // The other half of #1456: "who can reach this workspace", rendered on the
    // workspace access tab. New surface in this release, so it carries a guard
    // like the forward panel does.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erespaccess'))

    await page.goto(`/workspaces/${wsId}?tab=access`)
    const panel = page.getByTestId('resource-access')
    await expect(panel).toBeVisible({ timeout: 15_000 })
    await expectNoHorizontalPageScroll(page)
  })

  test('the reach panel fits a phone without scrolling the page sideways', async ({ page }) => {
    await page.goto('/admin/roles');
    await expectNoHorizontalPageScroll(page);

    await page.getByRole('button', { name: /create role/i }).click();
    const panel = page.getByTestId('role-reach');
    await expect(panel).toBeVisible();

    // Workspace names and label rules are both unbounded strings, so this is
    // the panel most likely to push a phone layout sideways.
    await page.fill('#r-allow-labels', 'env=production-eu-west-1-primary');
    await expectNoHorizontalPageScroll(page);
    await expect(panel).toBeVisible();
  });
});

test.describe('Vault diagnostics (#1663)', () => {
  // Stubbed: the E2E stack has no Vault. What is under test is the layout of
  // every state at phone width, with the primary signal never hidden.
  const status = {
    data: [
      {
        id: 'primary',
        type: 'vault-instance-statuses',
        attributes: {
          name: 'primary', default: true,
          address: 'https://vault-with-a-very-long-hostname.internal.example.test:8200',
          namespace: 'admin/team-with-a-long-namespace-name', 'auth-method': 'jwt',
          'auth-mount': 'jwt', 'auth-role': 'terrapod', 'tls-trust': 'global-bundle',
          reachable: false, initialized: null, sealed: null, standby: null, version: '',
          'health-error': 'ConnectError: [Errno 111] Connection refused while contacting the Vault health endpoint',
          'login-ok': false, 'login-error': 'Vault login failed for instance \'primary\' (jwt auth)',
          'ttl-seconds': null, 'checked-at': '2026-09-15T10:00:00Z',
          'last-error': {
            class: 'VaultUnavailable', at: '2026-09-15T09:59:00Z',
            message: "variables 'DB_PASSWORD', 'DB_USERNAME': Vault read of 'secret/apps/a/very/deep/path' failed",
          },
        },
      },
    ],
    meta: {
      pagination: { 'current-page': 1, 'page-size': 1, 'total-count': 1, 'total-pages': 1 },
      vault: { enabled: true, 'sampled-at': '2026-09-15T10:00:00Z', 'unavailable-reason': null },
    },
  }

  test('the Vault status page fits a phone and keeps its primary signal', async ({ page }) => {
    await page.route('**/api/terrapod/v1/admin/vault', (route: Route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(status) }),
    )
    await page.goto('/admin/vault')
    const card = page.getByTestId('vault-instance-primary').filter({ visible: true })
    await expect(card).toBeVisible()
    // Reachable and login are the two answers an operator opens this for;
    // neither may be dropped to make the card fit.
    await expect(card.getByText('Unreachable', { exact: true }).filter({ visible: true })).toBeVisible()
    await expect(card.getByText('Login failed', { exact: true }).filter({ visible: true })).toBeVisible()
    await expectNoHorizontalPageScroll(page)
  })

  test('the reference Check action and its result fit a phone', async ({ page }) => {
    const wsId = await createWorkspace(getStoredToken(), uniqueName('e2erespvcheck'), {
      'execution-mode': 'agent',
    })
    await page.route('**/api/terrapod/v1/vault/availability', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: {
            type: 'vault-availability', id: 'vault',
            attributes: { enabled: true, instances: ['primary'], 'default-instance': 'primary' },
          },
        }),
      }),
    )
    await page.route('**/vault-reference-checks', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          data: {
            id: 'vrc-1', type: 'vault-reference-checks',
            attributes: {
              ok: false, keys: ['a_rather_long_key_name_one', 'another_quite_long_key_name_two', 'three'],
              notes: [],
              checks: [
                { name: 'parses', status: 'pass', detail: '' },
                { name: 'readable', status: 'fail', detail: "the policy attached to role 'terrapod' grants [list] on 'secret/data/apps/a/very/deep/path'; the read needs read" },
              ],
            },
          },
        }),
      }),
    )

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    await page.getByRole('button', { name: 'Add Variable' }).filter({ visible: true }).first().click()
    await page.locator('#var-source').selectOption('vault')
    await page.locator('#add-mount').fill('secret')
    await page.locator('#add-path').fill('apps/a/very/deep/path')
    await page.locator('#add-field').fill('password')
    const check = page.getByRole('button', { name: 'Check', exact: true }).filter({ visible: true })
    await expect(check).toBeVisible()
    await check.click()

    const result = page.getByRole('status').filter({ visible: true })
    await expect(result.getByText('This reference will not resolve as it is.')).toBeVisible()
    await expect(result.getByText('Terrapod may read it')).toBeVisible()
    await expectNoHorizontalPageScroll(page)
  })
})
