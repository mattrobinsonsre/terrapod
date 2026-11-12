import { test, expect, type Page, type Route, type Dialog } from '@playwright/test';
// Lives in helpers/, not here: Playwright forbids a spec importing a spec, and
// any suite adding a surface should be able to reuse the mobile guard.
import { expectNoHorizontalPageScroll } from '../helpers/responsive';
import { expectRunKindOffered, queueRunKind } from '../helpers/run-kinds';
import { getStoredToken, createWorkspace, lockWorkspace, createUser, createAgentPool, createRegistryModule, seedRun, seedStateVersion, seedStateVersionWithContent, seedRunTask, seedVariable, seedInventoryHost, seedInventoryGroup, seedInventoryMembership, seedInventoryVar, uniqueName } from '../helpers/api';

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
  test('a two-tier hint is openable with a thumb and pushes rather than covers (#2413 F15)', async ({
    page,
  }) => {
    // The ⓘ is the SMALLEST control on a form full of inputs, so it is the one
    // most likely to be missed with a thumb -- hence `min-h-11 min-w-11` on
    // touch, collapsing on a precise pointer. Asserted here because this is
    // the only project running a coarse pointer.
    await page.goto('/workspaces')
    await page.click('button:has-text("New Workspace")')
    const toggle = page.getByTestId('hint-detail-toggle').first()
    await expect(toggle).toBeVisible({ timeout: 15_000 })

    const box = await toggle.boundingBox()
    expect(box).not.toBeNull()
    expect(box!.height).toBeGreaterThanOrEqual(44)
    expect(box!.width).toBeGreaterThanOrEqual(44)

    await expectNoHorizontalPageScroll(page)

    // Opening it must not introduce one either: the detail carries paths and
    // identifiers that do not wrap on their own, which is why it is
    // `break-words`. A floating popover would have covered the control it
    // describes at this width; an in-flow block can only push.
    await toggle.click()
    const detail = page.getByTestId('hint-detail').first()
    await expect(detail).toBeVisible()
    await expectNoHorizontalPageScroll(page)
    const db = await detail.boundingBox()
    expect(db).not.toBeNull()
    const vw = page.viewportSize()!.width
    expect(db!.x).toBeGreaterThanOrEqual(0)
    expect(db!.x + db!.width).toBeLessThanOrEqual(vw)
  })

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

  test('a variable keeps its category and value at phone width (#1898)', async ({ page }) => {
    // The category is primary signal — it is what says whether a value reaches
    // the engine or the process environment — so it must survive the narrow
    // layout rather than being one of the columns that gets hidden.
    //
    // This replaces a test for the `applies-to-engine` badge. That badge
    // existed because a variable could sit in a category its engine would never
    // read; with one category for every engine's parameters, that state cannot
    // arise and there is nothing left to flag.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erespvareng'))
    const seed = await fetch(`${API_URL}/api/v1/workspaces/${wsId}/vars`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/vnd.api+json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        data: {
          type: 'vars',
          attributes: { key: 'aws:region', category: 'native', value: 'eu-west-1' },
        },
      }),
    })
    expect(seed.status).toBe(201)

    await page.goto(`/workspaces/${wsId}?tab=variables`)
    const card = page.locator('li').filter({ hasText: 'aws:region' })
    await expect(card).toBeVisible({ timeout: 15_000 })
    // The engine's own word for the category, not the API value (#1898).
    await expect(card).toContainText('Terraform')
    await expect(card).toContainText('eu-west-1')
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
        data: { type: 'vcs-connections', attributes: { name, provider: 'gitlab', token: 'glpat-e2e-not-a-real-token' /* gitleaks:allow — fixture, not a credential */ } },
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

  test('a VCS connection states which repositories it may reach, at phone width (GHSA-v8g7-pqrj-8mcm)', async ({ page }) => {
    // Two connections, because the thing under test is a CONTRAST: an empty
    // allowlist means *any* repository, which is the opposite of what a blank
    // list usually implies. A single unrestricted fixture would pass however
    // the two states were worded, so one of each is seeded.
    const token = getStoredToken()
    const open = uniqueName('e2erespvcsopen')
    const shut = uniqueName('e2erespvcsshut')
    const ids: string[] = []

    async function seed(name: string, allowed: string[]) {
      const res = await fetch(`${API_URL}/api/terrapod/v1/vcs-connections`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'vcs-connections',
            attributes: {
              name,
              provider: 'gitlab',
              token: 'glpat-e2e-not-a-real-token' /* gitleaks:allow — fixture, not a credential */,
              'owner-email': 'owner@example.com',
              labels: { team: 'platform' },
              'allowed-repositories': allowed,
            },
          },
        }),
      })
      expect(res.ok).toBeTruthy()
      ids.push((await res.json()).data.id)
    }

    try {
      await seed(open, [])
      await seed(shut, ['example/infra-*', 'example/platform'])

      await page.goto('/admin/vcs-connections')
      await expect(page.getByText(open)).toBeVisible({ timeout: 15_000 })

      // Each card carries its own verdict, so locate within the card rather
      // than page-wide — page-wide would pass on either fixture's text.
      //
      // Scoped by test id, NOT by `locator('div').filter(...).last()`: that
      // matches every ancestor and descendant div whose subtree contains the
      // name, and `.last()` then takes the innermost — here the header div
      // holding the <h3>, which contains the name and none of the verdicts. It
      // failed as "element(s) not found" while the page rendered correctly.
      const card = (name: string) =>
        page.getByTestId('vcs-connection-card').filter({ hasText: name })
      const openCard = card(open)
      const shutCard = card(shut)
      await expect(openCard.getByText('Any repository is allowed')).toBeVisible()
      await expect(shutCard.getByText('Restricted to 2 repositories')).toBeVisible()
      // The patterns themselves are readable from the list — the question an
      // operator comes here to answer should not need a form opened.
      await expect(shutCard.getByText('example/infra-*')).toBeVisible()
      // The owner and the access labels are visible too.
      await expect(openCard.getByText('Owned by owner@example.com')).toBeVisible()
      await expect(openCard.getByText('platform')).toBeVisible()

      await expectNoHorizontalPageScroll(page)

      // The edit form's access controls have to be usable here, not just
      // present: a glob typed on a phone is the same security decision.
      await openCard.getByRole('button', { name: 'Edit' }).click()
      await expect(page.locator('#vcs-owner')).toHaveValue('owner@example.com')
      await expect(
        page.getByText('No patterns are listed, so this connection may be pointed at any repository.'),
      ).toBeVisible()

      const add = page.getByRole('button', { name: 'Add a pattern' })
      // A real tap target, not a run of coloured text (AGENTS.md → Responsive).
      expect((await add.boundingBox())!.height).toBeGreaterThanOrEqual(44)
      await add.click()
      const row = page.locator('input[placeholder="org/repo-*"]')
      await row.fill('example/only-this')
      // Adding the first pattern flips the stated posture from permissive to
      // restrictive — the UI must not keep claiming "any repository".
      await expect(page.getByText('1 pattern is listed', { exact: false })).toBeVisible()
      // Scoped to the pattern row, not `.first()` page-wide: the labels editor
      // renders above this one and its per-chip remove is also named "Remove …",
      // so `.first()` measured a different control than the one under test. (It
      // was 16px tall, which was a real defect and is fixed — but in the labels
      // editor, which this test is not about.)
      const remove = row.locator('xpath=..').getByRole('button', { name: 'Remove' }).first()
      expect((await remove.boundingBox())!.height).toBeGreaterThanOrEqual(44)

      await expectNoHorizontalPageScroll(page)
    } finally {
      for (const id of ids) {
        await fetch(`${API_URL}/api/terrapod/v1/vcs-connections/${id}`, {
          method: 'DELETE',
          headers: { Authorization: `Bearer ${token}` },
        })
      }
    }
  })

  test('both queue buttons are reachable at phone width (#1340)', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2eresp-queue'))

    await page.goto(`/workspaces/${wsId}?tab=runs`)

    // Plan-vs-apply is the decision that matters every time, so neither button
    // may be hidden behind a breakpoint — a phone must be able to make the
    // same choice a desktop can.
    await expectRunKindOffered(page, 'planApply')
    // Reached through the caret at phone width, which is the control this test
    // is really about: it is the smallest thing in the row and the one most
    // likely to be missed with a thumb, so it carries a 44px tap target.
    await expectRunKindOffered(page, 'plan')

    // Four buttons on one row is where a phone starts scrolling sideways.
    await expectNoHorizontalPageScroll(page)
  })

  test('the Configure button and its options are reachable at phone width (#1974)', async ({
    page,
  }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2eresp-configure'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'site.yml',
    })

    await page.goto(`/workspaces/${wsId}?tab=runs`)

    // A THIRD button on the row the #1340 test already calls the point where a
    // phone starts scrolling sideways — so this is the case that would break it.
    await expectRunKindOffered(page, 'configure')
    await expectNoHorizontalPageScroll(page)

    // And the per-run overrides: two more inputs inside the options panel,
    // which is where a monospace placeholder can push a phone wide.
    await page.getByRole('button', { name: 'Options', exact: true }).click()
    await expect(page.getByLabel('Limit', { exact: true })).toBeVisible()
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

  test('the security scanning panel is usable at phone width (#1763)', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2eresp-scanpanel'))

    await page.goto(`/workspaces/${wsId}`)

    // Three selects and a free-text field in one panel is the shape that
    // pushes a card sideways on a phone.
    const enforcement = page.getByLabel('Enforcement', { exact: true })
    await expect(enforcement).toBeVisible({ timeout: 15_000 })
    await expect(page.getByLabel('Scanner', { exact: true })).toBeVisible()
    await expect(page.getByLabel('Ignored rules')).toBeVisible()
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

    // The theme toggle is a full-size tap target labelled with what it will do.
    const html = page.locator('html');
    const startDark = /\bdark\b/.test((await html.getAttribute('class')) ?? '');
    const toggle = account.getByRole('button', { name: startDark ? 'Light theme' : 'Dark theme' });
    const box = await toggle.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
    await toggle.click();
    const back = account.getByRole('button', { name: startDark ? 'Dark theme' : 'Light theme' });
    await expect(back).toBeVisible();
    await back.click();
    await expect(account.getByRole('button', { name: startDark ? 'Light theme' : 'Dark theme' })).toBeVisible();
    // Let the cookie write settle so it cannot leak into the next test.
    await expect.poll(async () =>
      (await page.context().cookies()).find((c) => c.name === 'terrapod-theme')?.value,
    ).toBe(startDark ? 'dark' : 'light');
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

    await page.route(`**/api/*/runs/${runId}`, async (route: Route) => {
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

  test('the per-host log filter is usable at phone width (#2144)', async ({ page }) => {
    // A native <select>, for the same reason the plan index uses one: the
    // platform renders its own picker, so a fleet's worth of host names never
    // needs a custom menu that could overflow. Unlike that index this one does
    // NOT wait for the log to be whole, so the stub omits ETX -- the switch has
    // to work mid-run, which is the requirement.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('resp-hosts'));
    const runId = await seedRun(token, wsId);
    // One block per task per host (#2513). The host is a FIELD of the block
    // header, so a long name widens its own line rather than a column — which
    // is exactly the thing a phone width has to survive.
    const block = (host: string, status: string, label: string) =>
      `${'[10:28:30 +2.9s]'.padEnd(16)}  ${host.padEnd(16)}  ${status.padEnd(7)}  ${label}`;
    // A deliberately long host name: the one thing that could widen the page.
    const long = 'web-01.production.eu-west-1.internal.example.com';
    const body =
      '\x02#terrapod-host-log v2\n' +
      [
        `${'[2026-11-11 10:28:27]'.padEnd(16)}  ${'~'.padEnd(16)}  play     PLAY [configure]`,
        block('web1', 'ok', 'platform : say hello'),
        block(long, 'changed', 'platform : say hello'),
        `                  {"msg": "a long line from ${long} that must wrap rather than widen"}`,
      ].join('\n') + '\n';

    await page.route(`**/api/*/runs/${runId}`, async (route: Route) => {
      const res = await route.fetch();
      const json = await res.json();
      json.data.attributes.status = 'configuring';
      json.data.attributes['is-configure'] = true;
      // #1977's quiet-for indicator sits in this same crowded toolbar, so the
      // overflow assertion below has to be made with it rendered. The API
      // sends this only for a live configure, which is what the two lines
      // above make this run.
      json.data.attributes['last-output-at'] = new Date(Date.now() - 6 * 60_000).toISOString();
      await route.fulfill({ response: res, body: JSON.stringify(json) });
    });
    await page.route(`**/api/terrapod/v1/runs/${runId}/configure`, (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({
          data: {
            id: 'c',
            type: 'configures',
            attributes: { status: 'running', 'log-read-url': '/__e2e_cfg_log' },
          },
        }),
      }));
    await page.route('**/__e2e_cfg_log*', (route: Route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/plain',
        body: new URL(route.request().url()).searchParams.get('offset') === '0' ? body : '',
      }));

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=configure`);

    const picker = page.getByTestId('log-hosts-configure');
    await expect(picker).toBeVisible();
    // A real tap target, not a sliver of text.
    const box = await picker.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(24);
    // Selecting still works on touch, and the filtered view renders.
    await picker.selectOption(long);
    const pane = page.getByTestId('log-pre-configure').filter({ visible: true }).first();
    // The play block is host-less, so it survives the filter as the context it is.
    await expect(pane).toContainText('PLAY [configure]');
    // ...and the selected host's own block body comes with its block.
    await expect(pane).toContainText('that must wrap rather than widen');
    // #1977. Text rather than an eighth control, precisely because this row is
    // where a phone starts scrolling sideways -- so it is asserted present AND
    // inside the overflow check below rather than either alone.
    await expect(page.getByTestId('log-last-output-configure')).toBeVisible();

    // The whole point: a long host name must not widen the page -- now with
    // the quiet-for indicator in the same row.
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

    await page.route(`**/api/*/runs/${runId}`, async (route: Route) => {
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

  test('a run card lands on the phase that explains the result (#2413 F16)', async ({ page }) => {
    // The phone half of F16, and it is a DIFFERENT mechanism from the desktop
    // one rather than the same code at another width: `MobileCard` wraps its
    // whole body in a `<Link>`, so a separately-tappable status badge would be
    // interactive content nested inside an anchor. The card itself carries the
    // view instead -- one tap target, landing where the result is explained.
    //
    // Nothing is lost by going deeper: the run page's own view picker is a
    // native <select> at the top, asserted by the mobile run-page tests above.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('resp-f16'));
    await seedRun(token, wsId);

    // Stubbed, because a seeded run never starts a phase in a stack with no
    // runner -- so unstubbed the card correctly carries no view at all, which
    // is the negative case asserted at the end of this test.
    await page.route('**/api/*/workspaces/*/runs*', async (route: Route) => {
      const res = await route.fetch();
      const json = await res.json().catch(() => null);
      if (!Array.isArray(json?.data)) return route.fulfill({ response: res });
      for (const r of json.data) {
        r.attributes['status-timestamps'] = {
          'planning-at': '2026-01-01T00:00:00Z',
          'planned-at': '2026-01-01T00:01:00Z',
          'applying-at': '2026-01-01T00:02:00Z',
        };
      }
      await route.fulfill({ response: res, json });
    });

    await page.goto(`/workspaces/${wsId}?tab=runs`);
    const card = page.locator('a[href*="/runs/run-"]').first();
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card).toHaveAttribute('href', /\?view=apply$/);
    await expectNoHorizontalPageScroll(page);

    // And the negative: with no phase started the card must NOT invent one.
    await page.unroute('**/api/*/workspaces/*/runs*');
    await page.reload();
    const plain = page.locator('a[href*="/runs/run-"]').first();
    await expect(plain).toBeVisible({ timeout: 15_000 });
    await expect(plain).toHaveAttribute('href', /\/runs\/run-[0-9a-f-]+$/);
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

    // Outputs (#1568) sit below the version list, for either engine, and are
    // the first surface Terrapod has ever had for them. A seeded state
    // declares none, so this asserts the section is present and says so —
    // an empty state, never an error.
    await expect(page.getByRole('heading', { name: 'Outputs' })).toBeVisible({ timeout: 15_000 });

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

  test('touch: the Configure button prompts, and the prompt names the reach', async ({ page }) => {
    // The coarse-pointer half of the run buttons' tier (#2413 F14). On a
    // precise pointer the configure does NOT prompt — asserted in
    // `configure-trigger.spec.ts`, which is the decision: *"we're not having
    // confirm prompts on those buttons on normal screens."* On touch, where a
    // mis-tap is easy, it does. This Pixel project is the only proof of that
    // path, since the maintainer does not test on a real device.
    //
    // The reach is on the PAGE at this width too, which is what made the
    // precise-pointer relaxation safe rather than lossy — so both are asserted
    // here, in the one place where the dialog and the line coexist.
    const token = getStoredToken();
    const wsId = await createWorkspace(token, uniqueName('cfg-touch'), {
      'configure-enabled': true,
      'config-directory': 'ansible',
      'default-playbook': 'site.yml',
      'default-limit': 'canary',
    });

    await page.goto(`/workspaces/${wsId}?tab=runs`);
    const reach = page.getByTestId('ws-configure-reach');
    await expect(reach).toBeVisible({ timeout: 15_000 });
    await expect(reach).toContainText('canary');
    await expectNoHorizontalPageScroll(page);

    // Registered BEFORE the click, for the reason the test above gives:
    // confirm() is synchronous and blocks the handler.
    let msg = '';
    page.once('dialog', async (d) => { msg = d.message(); await d.dismiss(); });
    await queueRunKind(page, 'configure');
    // Names the limit rather than shouting about the whole estate — the
    // contrast that makes the warning worth reading.
    await expect.poll(() => msg, { timeout: 5_000 }).toContain('canary');
    expect(msg).not.toContain('WHOLE');
  });

  test('catalog browse page renders without horizontal scroll at phone width', async ({ page }) => {
    // The catalog browse page is a responsive card grid (or an empty state);
    // either way it must not scroll horizontally on a phone.
    await page.goto('/catalog');
    await expect(page.getByRole('heading', { name: 'Service Catalog' })).toBeVisible({ timeout: 15_000 });
    await expectNoHorizontalPageScroll(page);
  });

  test('the catalog admin form fits a phone with the Ansible group open (#2019)', async ({
    page,
  }) => {
    // `responsive.spec.ts` had NO coverage of /admin/catalog, while
    // `catalog.spec.ts` covers it at desktop width only — so the mobile guard
    // did not reach this form either before or after the Ansible group landed
    // on it.
    //
    // Locators read off the component, and ids where the component has them,
    // following `catalog.spec.ts`: the directory's own <label> carries a `*`
    // inside it, so its accessible name is not the label text.
    await page.goto('/admin/catalog');
    await expect(page.locator('h1:has-text("Catalog Admin")')).toBeVisible({ timeout: 15_000 });
    await page.click('button:has-text("New Catalog Item")');

    // The three paths render only while the checkbox is on
    // (`{f.configureEnabled && …}`), so this is also what makes the assertion
    // bite: without the click the form would fit a phone by not having the new
    // controls at all, which is the shape of pass that proves nothing.
    await expect(page.getByText('Ansible configure')).toBeVisible();
    await page.getByText('Provision a configure operation').click();
    await expect(page.locator('#cat-ansible-dir')).toBeVisible();
    await expect(page.locator('#cat-playbook')).toBeVisible();

    // A long value in the widest of the three, because the overflow this
    // guards against is content-driven: the group is a plain <div> rather than
    // a <fieldset> precisely so it can shrink, and a fieldset's
    // `min-inline-size: min-content` is how a phone-width overflow shipped
    // here once before.
    await page.fill('#cat-ansible-dir', 'deploy/ansible/environments/production/playbooks');
    await expectNoHorizontalPageScroll(page);
  });

  test('catalog admin editor fits a phone with backend constraints and template warning', async ({ page }) => {
    const item = { id: 'cat-0198e2e0-0000-7000-8000-00000000c030', type: 'catalog-items', attributes: {
      name: 'phone-catalog-item', 'display-name': 'Phone catalog item', description: '', enabled: true,
      'self-contained': false, 'execution-backend-constraints': null,
      'module-id': 'mod-phone', 'module-name': 'network', 'module-provider': 'aws',
      'default-version-pin': null, 'provider-template-ids': ['template-phone'],
      'allowed-agent-pool-ids': null, 'variable-options': [], labels: {}, 'owner-email': '',
    } };
    await page.route((url) => url.pathname === '/api/terrapod/v1/catalog-items', (route) => route.fulfill({ json: {
      data: [item],
    } }));
    await page.route(
      (url) => url.pathname === `/api/terrapod/v1/catalog-items/${item.id}` && url.searchParams.get('include-sensitive-values') === 'true',
      (route) => route.fulfill({ json: { data: item } }),
    );
    await page.goto('/admin/catalog');
    await expect(page.getByRole('heading', { name: 'Catalog Admin' })).toBeVisible();
    await page.getByRole('button', { name: 'Edit' }).click();
    await page.getByLabel('Self-contained root archive').check();
    await page.getByLabel('OpenTofu', { exact: true }).check();
    await page.locator('#cat-tofu-range').fill('>=1.12,<2');
    await expect(page.getByText('Saving removes existing provider-template associations.')).toBeVisible();
    await expect(page.locator('#cat-tofu-range')).toBeVisible();
    await expectNoHorizontalPageScroll(page);
  });

  test('a catalog item page fits a phone: provision form, interface and instances', async ({ page }) => {
    // The item, its form, interface and instances are stubbed so the page has
    // every section populated — including an instances table, which must
    // scroll inside its own container rather than widen the page.
    const itemId = 'cat-0198e2e0-0000-7000-8000-00000000c001';
    const base = `/api/terrapod/v1/catalog-items/${itemId}`;
    const varsPath = '/api/v1/workspaces/ws-0198e2e0-0000-7000-8000-00000000c002/vars';
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
              { name: 'terrapod_token', type: 'string', description: '', required: true, sensitive: true, default: null, options: null, source: 'module' },
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
              'catalog-update-block': { version: '2.0.0', reason: 'inputs' },
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
      (url) => url.pathname in stubs || url.pathname === '/api/terrapod/v1/catalog-instances/ws-0198e2e0-0000-7000-8000-00000000c002' || url.pathname === varsPath,
      (route) => {
        const url = new URL(route.request().url());
        if (url.pathname === varsPath) {
          return route.fulfill({ json: { data: [
            { id: 'var-cidr', attributes: { key: 'cidr_block_for_the_primary_network', value: '10.1.0.0/16', sensitive: false, category: 'native', 'value-source': 'static' } },
            { id: 'var-token', attributes: { key: 'terrapod_token', value: null, sensitive: true, category: 'native', 'value-source': 'static' } },
          ], meta } });
        }
        if (url.pathname.endsWith('/catalog-instances/ws-0198e2e0-0000-7000-8000-00000000c002')) {
          return route.fulfill({ json: { data: { attributes: {
            'input-values': { cidr_block_for_the_primary_network: '10.0.0.0/16' },
          } } } });
        }
        if (url.pathname === `${base}/form` && url.searchParams.get('version-pin') === 'latest') {
          return route.fulfill({ json: { data: { attributes: {
            'resolved-version': '2.0.0', fields: [
              { name: 'new_network_name', type: 'string', description: '', required: true, sensitive: false, default: null, options: null, source: 'module' },
            ],
          } } } });
        }
        return route.fulfill({ json: stubs[url.pathname] });
      },
    );

    await page.goto(`/catalog/${itemId}`);
    await expect(
      page.getByRole('heading', { name: 'A network with a rather long display name to wrap on a phone' }),
    ).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('#prov-name')).toBeVisible();
    const describedField = page.locator('#prov-cidr_block_for_the_primary_network');
    const plainField = page.locator('#prov-environment');
    await expect(describedField).toBeVisible();
    await expect(plainField).toBeVisible();
    await expect(page.locator('label[for="prov-terrapod_token"]')).toContainText('sensitive');
    const describedBox = await describedField.boundingBox();
    const plainBox = await plainField.boundingBox();
    expect(describedBox).not.toBeNull();
    expect(plainBox).not.toBeNull();
    expect(plainBox!.y).toBeGreaterThan(describedBox!.y + describedBox!.height);
    await expect(page.getByText('e2e-network-instance-with-a-long-name')).toBeVisible();
    await expectNoHorizontalPageScroll(page);
    await page.getByRole('button', { name: 'Reconfigure' }).click();
    await expect(page.getByText('Version 2.0.0 needs reconfiguration before it can run.')).toHaveCount(2);
    await expect(page.locator('#recfg-cidr_block_for_the_primary_network')).toBeVisible();
    await expect(page.locator('#recfg-cidr_block_for_the_primary_network')).toHaveValue('10.1.0.0/16');
    await expect(page.locator('label[for="recfg-terrapod_token"]')).toContainText('sensitive');
    await expect(page.locator('#recfg-terrapod_token')).toHaveValue('');
    const input = page.locator('#recfg-cidr_block_for_the_primary_network');
    await input.focus();
    await page.keyboard.type('x');
    await expect(input).toBeFocused();
    await page.keyboard.type('y');
    await expect(input).toBeFocused();
    await page.selectOption('#recfg-version', '');
    await expect(page.locator('#recfg-new_network_name')).toBeVisible();
    await expect(page.getByText('No longer used by this version')).toBeVisible();
    await expectNoHorizontalPageScroll(page);
    await page.getByRole('button', { name: 'Cancel' }).click();
    await page.route((url) => url.pathname === varsPath, (route) => route.fulfill({ status: 403 }));
    await page.getByRole('button', { name: 'Reconfigure' }).click();
    await expect(page.locator('#recfg-cidr_block_for_the_primary_network')).toHaveValue('10.0.0.0/16');
    await expect(page.locator('#recfg-terrapod_token')).toHaveValue('');
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

test.describe('Runner debug mode (#1764)', () => {
  test('the banner and toggle fit a phone and keep their signal', async ({ page }) => {
    // The banner carries two long sentences of security copy, which is the
    // shape most likely to push a phone layout sideways — and the one that
    // must not be dropped at narrow widths, because it is the only place an
    // operator is told that failed pods are holding this workspace's
    // credentials.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erespdebug'))

    await page.goto(`/workspaces/${wsId}`)
    const toggle = page.getByLabel('Debug mode', { exact: true })
    await expect(toggle).toBeVisible({ timeout: 15_000 })
    await expectNoHorizontalPageScroll(page)

    // Tier 2 of the #719 confirm policy, and only in the ON direction: turning
    // debug mode on is what starts holding credentials. The handler is
    // registered BEFORE the click because window.confirm() blocks the handler.
    let msg = ''
    page.once('dialog', async (d) => { msg = d.message(); await d.accept() })
    await toggle.click()
    await expect.poll(() => msg, { timeout: 5_000 }).toContain('debug mode')
    await expect(toggle).toBeChecked({ timeout: 15_000 })

    await expect(page.getByTestId('debug-mode-banner')).toBeVisible({ timeout: 15_000 })
    await expectNoHorizontalPageScroll(page)

    // Turning it back off is the safe direction, so it must NOT prompt --
    // a confirm in front of "stop holding credentials" trains people to
    // dismiss the one that matters.
    let offDialogFired = false
    const spy = async (d: Dialog) => { offDialogFired = true; await d.dismiss() }
    page.on('dialog', spy)
    await toggle.click()
    await expect(toggle).not.toBeChecked({ timeout: 15_000 })
    await expect(page.getByTestId('debug-mode-banner')).toHaveCount(0)
    expect(offDialogFired).toBe(false)
    page.off('dialog', spy)
  })
})

test.describe('AI policy gate (#1766)', () => {
  test('the per-workspace override and its caveat fit a phone', async ({ page }) => {
    // The caveat is the reason this needs a responsive assertion at all: it is
    // a long sentence saying that "Never" does NOT escape a mandatory gate,
    // and it sits under a select inside a two-column grid. If it is what
    // pushes the page sideways, the fix must not be to drop it — someone who
    // sets this believing they have left a fleet-wide blocking control is
    // exactly who the line is for.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erespaipol'))

    await page.goto(`/workspaces/${wsId}`)
    const select = page.getByTestId('ai-policy-mode')
    await expect(select).toBeVisible({ timeout: 15_000 })
    await expectNoHorizontalPageScroll(page)

    // The caveat is present at phone width, not hidden to make the grid fit.
    await expect(page.getByText(/mandatory gate ignores/i)).toBeVisible()

    // Changing it is a reversible settings write, not a single-tap mutation,
    // so tier 2 of the #719 confirm policy does not apply and it must NOT
    // prompt — a confirm here trains people to dismiss the ones that matter.
    let dialogFired = false
    const spy = async (d: Dialog) => { dialogFired = true; await d.dismiss() }
    page.on('dialog', spy)
    await select.selectOption('disabled')
    await expect(select).toHaveValue('disabled', { timeout: 15_000 })
    expect(dialogFired).toBe(false)
    page.off('dialog', spy)

    await expectNoHorizontalPageScroll(page)
  })
})

test.describe('Per-workspace run identity (#1901)', () => {
  // The mobile guard for the three surfaces #1901 adds the audience editor to.
  // The value is a MAP of provider configuration -> audiences, so each entry is
  // a card holding its own key, a provenance badge and its own list of rows,
  // and an audience is a long opaque string (`api://AzureADTokenExchange`) —
  // exactly the shape that pushes a page sideways when a value is allowed to
  // set a card's width.
  //
  // What this suite CANNOT reach: the inherited half. A workspace read returns
  // the deployment catalogue merged over the workspace's own map, so an entry
  // only reads as inherited when the deployment configures one — and
  // `auth.oidc_issuer.audiences` defaults to empty with the issuer OFF, which
  // the e2e stack does not override. Configuring it is a compose/Helm change,
  // so the subtraction is pinned by unit test (`web/tests/oidc-audiences.test.ts`)
  // and what is asserted here is the half this stack can actually produce:
  // every entry workspace-owned, and the issuer-disabled notice shown rather
  // than an empty editor that looks configurable.

  test('the workspace audience map reads and edits at phone width', async ({ page }) => {
    const token = getStoredToken()
    // Two entries, and the second is ALIASED: `aws.west` is one key, never
    // split on the dot, so the read view has to show it whole.
    const wsId = await createWorkspace(token, uniqueName('e2erespoidc'), {
      'oidc-audiences': {
        aws: ['sts.amazonaws.com'],
        'aws.west': ['api://AzureADTokenExchange'],
      },
    })

    // Read-only: both the provider keys and every audience stay visible, not
    // hidden behind a breakpoint to make the grid fit. Someone checking which
    // audiences a workspace mints for on a phone is the whole point.
    await page.goto(`/workspaces/${wsId}`)
    const shown = page.getByTestId('oidc-audiences')
    await expect(shown).toBeVisible({ timeout: 15_000 })
    await expect(shown).toContainText('aws.west')
    await expect(shown).toContainText('api://AzureADTokenExchange')
    await expectNoHorizontalPageScroll(page)

    // Editing it is a reversible settings write, not a single-tap mutation, so
    // tier 2 of the #719 confirm policy does not apply and it must NOT prompt.
    let dialogFired = false
    const spy = async (d: Dialog) => { dialogFired = true; await d.dismiss() }
    page.on('dialog', spy)

    // Provenance is rendered, not inferred from absence: this stack configures
    // no catalogue, so both entries are the workspace's own and both say so.
    // A badge on each side is what makes "which of these did I set" answerable
    // at a glance, and the read view is where someone asks it.
    await expect(shown.getByText('Workspace', { exact: true }).first()).toBeVisible()
    await expect(shown.getByText('Deployment default')).toHaveCount(0)

    await page.getByRole('button', { name: 'Edit' }).first().click()

    // Each existing entry carries its own audience list, so 'Add audience'
    // belongs to a card while 'Add provider configuration' is the top-level one.
    // The editor appears only once the catalogue probe has settled — before
    // that the partition would mark every inherited entry as owned — so this
    // wait is load-bearing, not incidental.
    const addProvider = page.getByRole('button', { name: 'Add provider configuration' })
    await expect(addProvider).toBeVisible({ timeout: 15_000 })
    await expect(page.getByRole('button', { name: 'Add audience' }).first()).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    // The issuer is off by default, so the editor says it is inert rather than
    // letting an operator configure something that can never be minted for.
    await expect(page.getByTestId('oidc-issuer-disabled')).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    // A row's action is a real button with a tap target, not bare coloured
    // text, and adding a third entry keeps the page inside the viewport.
    //
    // Counted by the per-card remove's OWN accessible name, not by the word
    // "Remove": that button carries an aria-label naming its provider, which
    // overrides its text content, so `{ name: 'Remove' }` matches the
    // per-AUDIENCE removes inside each card instead and counts rows rather
    // than cards. Asserting the aria-label is also the stronger check, since
    // it is what a screen-reader user hears for a control that would otherwise
    // be one of several identical "Remove"s on the page.
    const removeProvider = page.getByRole('button', {
      name: /^Remove the .+ provider configuration$/,
    })
    await expect(removeProvider).toHaveCount(2)
    await expect(addProvider).toBeDisabled()
    await page.getByTestId('oidc-provider-input').fill('vault')
    await expect(addProvider).toBeEnabled()
    await addProvider.click()
    await expect(removeProvider).toHaveCount(3)
    await expect(
      page.getByRole('button', { name: 'Remove the vault provider configuration' })
    ).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    expect(dialogFired).toBe(false)
    page.off('dialog', spy)
  })

  test('the fleet form gates the list behind a checkbox and fits a phone', async ({ page }) => {
    // The gate is load-bearing, not decoration: an empty list is a real value
    // here (it turns run identity off), so it cannot also mean "leave alone".
    await page.goto('/admin/bulk-update')
    await expectNoHorizontalPageScroll(page)

    // The checkbox INSIDE its label, not the label's text. This checkbox has no
    // `aria-label` and would be named by the label wrapping it, while the one
    // checkbox this suite already finds by name carries an explicit one -- so
    // that is no precedent. `.check()` also drives the input and waits for
    // actionability, where clicking the long label row did not.
    const gate = page
      .locator('label', { hasText: /Set OIDC run identity audiences/i })
      .locator('input[type="checkbox"]')
    await expect(gate).toBeVisible({ timeout: 15_000 })

    // An empty map has no entries, so the only control the gate reveals is the
    // top-level one; 'Add audience' lives inside an entry and does not exist yet.
    const add = page.getByRole('button', { name: 'Add provider configuration' })
    await expect(add).toHaveCount(0)

    // Retried, and only while the list is still closed, so a handler that
    // hydrates after the first click cannot toggle it back shut.
    await expect(async () => {
      if ((await add.count()) === 0) await gate.check()
      await expect(add).toBeVisible({ timeout: 1_000 })
    }).toPass({ timeout: 15_000 })

    await expectNoHorizontalPageScroll(page)

    // Naming a provider reveals that entry's own audience list, still inside
    // the viewport once a card and its row are both on the page.
    await page.getByTestId('oidc-provider-input').fill('aws.west')
    await add.click()
    await expect(page.getByRole('button', { name: 'Add audience' })).toBeVisible()
    // A fleet template is a pure override with nothing to merge against, so it
    // has no provenance to report. The badge the workspace read view carries
    // must NOT appear here, or the form asserts an ownership it cannot know --
    // the defect `showProvenance` was added to fix, and this is its only guard.
    // Scoped to the editor: a bare negative on an admin page could pass or fail
    // on any other element that happens to render the exact word.
    const editor = page.getByTestId('oidc-audience-editor')
    await expect(editor.getByText('Workspace', { exact: true })).toHaveCount(0)
    await expect(editor.getByText('Deployment default')).toHaveCount(0)
    await expectNoHorizontalPageScroll(page)
  })

  test('the autodiscovery rule form carries the audience list at phone width', async ({ page }) => {
    await page.goto('/admin/autodiscovery')
    await expectNoHorizontalPageScroll(page)

    // One thing has to happen before the editor exists: open the form. There
    // used to be a second -- expanding the collapsed "Workspace template
    // defaults" <details> that every templated field lived inside -- and
    // #2413 F4 removed it, because sections carry navigability and collapse
    // does not. The click is gone rather than left as a tolerated no-op: a
    // guarded step for an element that cannot exist reads as a section nobody
    // has checked for.
    //
    // The retry stays, and is about the click rather than the section: a
    // client-component button right after `page.goto` can lose a pre-hydration
    // click, and it is guarded on the editor still being absent so a second
    // click cannot close a form the first one opened.
    const add = page.getByRole('button', { name: 'Add provider configuration' })
    await expect(async () => {
      if ((await add.count()) > 0) {
        await expect(add).toBeVisible({ timeout: 1_000 })
        return
      }
      const newRule = page.getByRole('button', { name: 'New Rule' })
      if ((await newRule.count()) > 0) await newRule.click()
      await expect(add).toBeVisible({ timeout: 1_000 })
    }).toPass({ timeout: 20_000 })

    await expectNoHorizontalPageScroll(page)

    // An aliased key is one key: typing the dot must not split it into a
    // nested anything, and the card's heading carries it whole.
    await page.getByTestId('oidc-provider-input').fill('vault.eu')
    await add.click()
    await expect(page.getByRole('button', { name: 'Remove' }).first()).toBeVisible()
    await expect(page.getByText('vault.eu', { exact: true })).toBeVisible()
    await expectNoHorizontalPageScroll(page)
  })

})

// Its own describe: the pickers are the Ansible execution-environment feature,
// not part of the run-identity work above. This file already carries a note
// about a test that ended up in the wrong describe by accident.
test.describe('Ansible execution environments (#2154)', () => {

  // The execution-environment pickers (#2154). Both cases are driven here
  // because the whole point of the server reporting `meta.listing` is that the
  // form behaves DIFFERENTLY in the two states, and a single fixture cannot
  // see a difference -- which is the shape of weak test this file has been
  // bitten by before.
  for (const listing of ['complete', 'unknown'] as const) {
    test(`the execution-environment fields follow meta.listing=${listing} at phone width`, async ({
      page,
    }) => {
      await page.route(
        (url) => url.pathname.endsWith('/ansible/execution-environments'),
        (route) =>
          route.fulfill({
            json: {
              data: [
                {
                  type: 'ansible-execution-environments',
                  id: '2.21',
                  attributes: {
                    kind: 'built-in',
                    'ansible-core-line': '2.21',
                    label: null,
                    image: 'r/ee:2.21',
                  },
                },
                {
                  type: 'ansible-execution-environments',
                  id: 'rhel8-hardened',
                  attributes: {
                    kind: 'operator',
                    'ansible-core-line': null,
                    label: 'rhel8-hardened',
                    image: 'registry.internal/ee:1',
                  },
                },
              ],
              meta: { listing },
            },
          }),
      )

      await page.goto('/admin/autodiscovery')
      await expectNoHorizontalPageScroll(page)

      // Same single step and the same reasoning as the test above: open the
      // form. Nothing collapses on it any more (#2413 F4).
      //
      // Both pickers are VISIBLE AND DISABLED here, and that is the point of
      // #2413 F20 rather than an accident of the fixture: they belong to the
      // Ansible group, every field of which gates on `configure-enabled`, and
      // a brand-new rule has configure OFF. Seeing the option is how an
      // operator learns it exists. So this asserts the ROLE and the offered
      // set -- what the form is telling them -- and deliberately not that
      // either control is editable in this state.
      const eeSelect = page.getByTestId('rule-ansible-ee')
      await expect(async () => {
        if ((await eeSelect.count()) > 0) {
          await expect(eeSelect).toBeVisible({ timeout: 1_000 })
          return
        }
        const newRule = page.getByRole('button', { name: 'New Rule' })
        if ((await newRule.count()) > 0) await newRule.click()
        await expect(eeSelect).toBeVisible({ timeout: 1_000 })
      }).toPass({ timeout: 20_000 })

      // The operator label is ALWAYS a picker, in both states: labels come from
      // configuration rather than from a registry, so they are authoritative
      // even when the built-in listing is not.
      await expect(eeSelect).toHaveRole('combobox')
      await expect(eeSelect.locator('option[value="rhel8-hardened"]')).toHaveCount(1)

      // The line field is a picker ONLY when the set is known. With `unknown`
      // the server accepts any line, so an empty-or-stale picker would block a
      // value the server would have taken -- it must be free text.
      const line = page.getByTestId('rule-ansible-version')
      await expect(line).toBeVisible()
      await expect(line).toHaveRole(listing === 'complete' ? 'combobox' : 'textbox')
      if (listing === 'complete') {
        await expect(line.locator('option[value="2.21"]')).toHaveCount(1)
      }

      await expectNoHorizontalPageScroll(page)
    })
  }

})

// The inventory tab is its own feature, not part of the run-identity work the
// block above covers; it was sitting in that describe by accident.
test.describe('Workspace inventory (#1967, #1968, #1969)', () => {
  test('the operations and Ansible configure fields hold up at phone width (#1929, #1966)', async ({
    page,
  }) => {
    // The six roots and run defaults moved onto the workspace Configuration tab
    // (#1929), and the two operation booleans joined them above (#1966). Both
    // arrived as nested grids inside a `dd` — a grid item's `min-width` is
    // `auto`, so a control whose label is a long translated string pushes the
    // row past the viewport however `w-full` it is. That is the
    // `min-inline-size` lesson in its other costume, and the reason this is
    // driven in EDIT mode: a read-only `dl` of short values cannot overflow and
    // so proves nothing about the controls.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erespcfg'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)
    await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    await page.getByRole('button', { name: 'Edit', exact: true }).first().click()

    // The two operation checkboxes and their hints, which are full sentences in
    // every locale. Their rows carry `min-h-11` so the tap target is 44px at
    // this width — a bare 20px checkbox would not be one.
    const planApply = page.getByLabel('Plan and apply', { exact: true })
    const configure = page.getByLabel('Configure', { exact: true })
    await expect(planApply).toBeVisible()
    await expect(configure).toBeVisible()
    for (const box of [planApply, configure]) {
      const h = (await box.locator('xpath=ancestor::label[1]').boundingBox())?.height ?? 0
      expect(h).toBeGreaterThanOrEqual(44)
    }
    await expectNoHorizontalPageScroll(page)

    // The both-false banner is the widest thing this block can show, and it
    // only exists while the pair is refused — so it is driven rather than
    // waited for.
    await planApply.uncheck()
    await expect(page.getByTestId('ws-operations-none')).toBeVisible()
    await expectNoHorizontalPageScroll(page)
    await planApply.check()

    // The config directory is a plain text box since #1966 retired its mode
    // select, and it is always present rather than revealed.
    await expect(page.getByPlaceholder('e.g. ansible')).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    // A long value in the list editor is the realistic overflow case: an
    // inventory path has no spaces to wrap on.
    await page.getByRole('button', { name: 'Add inventory path' }).click()
    await page
      .getByPlaceholder('e.g. inventories/production')
      .first()
      .fill('inventories/production/group_vars/all/very-long-unbroken-path-segment')
    await expectNoHorizontalPageScroll(page)
  })

  test('the Inventory tab holds up at phone width (#1967, #1968, #1969)', async ({ page }) => {
    // Four sub-views, and every one of them is a phone-width hazard: two
    // multi-column tables of rows, a variable table whose values have no spaces
    // to wrap on, a text input beside its button, and a run of resolved host
    // variables. All four are walked here.
    //
    // The fifth used to be the settings form, a `<fieldset>` — which defaults to
    // `min-inline-size: min-content` and will not shrink below its content
    // unless told to, which is how /admin/bulk-update once overflowed by 122px.
    // That form is gone (#1929); every fieldset the application still has is on
    // /admin/bulk-update, which this suite drives separately, so the case is
    // still covered. Three of them since #2413 F4 — the filter, and the two
    // that disable a list editor behind an off gate — and all three carry the
    // `min-w-0` that is the whole fix, so "only remaining" is no longer the
    // right wording even though the reasoning is unchanged.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erespinv'))
    const web = await seedInventoryGroup(token, wsId, 'web')
    const h1 = await seedInventoryHost(token, wsId, 'web-1')
    const h2 = await seedInventoryHost(token, wsId, 'web-2')
    await seedInventoryMembership(token, web, h1)
    await seedInventoryMembership(token, web, h2)
    // Long enough to push a narrow container sideways if nothing wraps it.
    await seedInventoryVar(token, { host: h1 }, 'ansible_python_interpreter', '/usr/bin/python3')
    await seedInventoryVar(token, { workspace: wsId }, 'ansible_user', 'deploy')

    await page.goto(`/workspaces/${wsId}?tab=inventory`)
    await expect(page.getByRole('heading', { name: 'Hosts', exact: true })).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    // The primary signal survives the reflow. A card list that dropped the
    // group or variable counts would still pass the overflow check on its own.
    //
    // Scoped to what is VISIBLE, not `.first()`. Both renders sit in the DOM at
    // every width — the desktop table is `hidden md:block` and the card list is
    // `md:hidden` — so `.first()` takes the table's cell in DOM order and then
    // asserts a hidden element is visible.
    await expect(page.getByText('web-1').filter({ visible: true }).first()).toBeVisible()
    await expect(page.getByText('web-2').filter({ visible: true }).first()).toBeVisible()
    const card = page.locator('li', { hasText: 'web-1' }).filter({ visible: true }).first()
    await expect(card.getByText('Groups')).toBeVisible()
    await expect(card.getByText('Variables')).toBeVisible()
    // Row actions are real buttons with a background, not clickable text, so
    // they are tappable at this width.
    await expect(card.getByRole('button', { name: 'Edit', exact: true })).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    // The sub-view nav wraps rather than scrolling inside itself: an inner
    // scroller hides whichever entry the reader needs.
    // `Inventory variables`, not `Variables` (#2413 F7): the pill and the
    // workspace tab strip both read "Variables", in two idioms, going to
    // different places, so the sub-view one was relabelled.
    for (const view of ['Groups', 'Inventory variables', 'Resolved']) {
      await page.getByRole('button', { name: view, exact: true }).click()
      await expectNoHorizontalPageScroll(page)
    }

    // The `<fieldset>` case used to be driven here, via the inventory settings
    // form. That form is gone (#1929) and every fieldset the application still
    // has is on `/admin/bulk-update`, which this suite already drives — so no
    // coverage is lost by removing this block rather than relocating it.

    // The variable table, whose values are unbroken identifier-ish runs.
    await page.getByRole('button', { name: 'Inventory variables', exact: true }).click()
    await expect(page.getByText('ansible_user').filter({ visible: true }).first()).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    // The limit control is what this tab exists to offer before anything runs,
    // so it has to be usable on a phone: typable input, tappable button.
    await page.getByRole('button', { name: 'Resolved', exact: true }).click()
    const input = page.getByLabel('Limit pattern')
    await expect(input).toBeVisible()
    await input.fill('web:!web-2')
    await expect(input).toHaveValue('web:!web-2')
    await expect(page.getByRole('button', { name: 'Apply limit', exact: true })).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    // The resolution is live, so there is no Refresh to mis-tap.
    await expect(page.getByRole('button', { name: /refresh/i })).toHaveCount(0)
  })

  test('removing a group membership prompts a touch confirm (#1969)', async ({ page }) => {
    // Tier 2 of the #719 policy: a reversible single-tap mutation prompts on a
    // COARSE pointer, where a mis-tap is easy, and proceeds without one on a
    // precise pointer (asserted in confirm-guards.spec.ts and inventory.spec.ts).
    // A membership removal is the reversible case — the link can be recreated —
    // so it must not be promoted to the unconditional delete tier either.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2erespinvmem'))
    const web = await seedInventoryGroup(token, wsId, 'web')
    const h1 = await seedInventoryHost(token, wsId, 'web-1')
    await seedInventoryMembership(token, web, h1)

    await page.goto(`/workspaces/${wsId}?tab=inventory&inv=${h1}`)
    await expect(page.getByRole('heading', { name: 'Host web-1' })).toBeVisible()

    let message = ''
    page.once('dialog', async (d: Dialog) => {
      message = d.message()
      await d.accept()
    })
    await page.getByRole('button', { name: /Remove this host from web/ }).click()
    await expect.poll(() => message, { timeout: 5_000 }).toContain('web')
    await expect(page.getByText(/in no group/i)).toBeVisible({ timeout: 10_000 })
    await expectNoHorizontalPageScroll(page)
  })
})

test.describe('Phase-slot variables (#2189, #2190)', () => {
  test('a long key plus its apply-only badge does not push the page sideways', async ({ page }) => {
    // The badge sits beside the KEY rather than next to the category pill,
    // because the category column is `hidden sm:table-cell` -- invisible at
    // exactly this width, where two rows sharing a name are most confusing.
    // That puts a new inline element next to the longest string on the row, so
    // the thing to prove is that it wraps rather than overflowing.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2e-resp-applyonly'))
    const longKey = 'AWS_ASSUME_ROLE_ARN_FOR_THE_PRODUCTION_DEPLOYMENT_PIPELINE'
    await seedVariable(token, wsId, longKey, 'arn:plan', false)
    await seedVariable(token, wsId, longKey, 'arn:apply', true)

    await page.goto(`/workspaces/${wsId}?tab=variables`)

    // The mobile card list is what renders here; the table is hidden md:block.
    // Scope to the visible render, or the assertion passes on a hidden node.
    const badge = page.getByText('Apply only', { exact: true }).filter({ visible: true })
    await expect(badge).toHaveCount(1, { timeout: 10_000 })
    await expect(badge).toBeVisible()

    // Both copies are reachable: without the badge they are indistinguishable,
    // and at this width the category pill that would otherwise tell them apart
    // is not rendered at all.
    await expect(page.getByText('arn:plan', { exact: true }).filter({ visible: true })).toBeVisible()
    await expect(page.getByText('arn:apply', { exact: true }).filter({ visible: true })).toBeVisible()

    await expectNoHorizontalPageScroll(page)
  })

  test('a configure-only badge beside a long key does not push the page sideways', async ({
    page,
  }) => {
    // Three slots means a key can now carry three rows, so the same wrapping
    // question applies to a second inline badge -- and at this width the
    // category pill that would otherwise tell the rows apart is not rendered.
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2e-resp-cfgonly'))
    const longKey = 'ANSIBLE_BECOME_PASSWORD_FOR_THE_PRODUCTION_FLEET_ROLLOUT'
    await seedVariable(token, wsId, longKey, 'ordinary', false, false)
    await seedVariable(token, wsId, longKey, 'fleet-secret', false, true)

    await page.goto(`/workspaces/${wsId}?tab=variables`)

    const badge = page.getByText('Configure only', { exact: true }).filter({ visible: true })
    await expect(badge).toHaveCount(1, { timeout: 10_000 })
    await expect(badge).toBeVisible()

    await expectNoHorizontalPageScroll(page)
  })
})

test.describe('Per-host results (#1928)', () => {
  test('a four-host configure table does not push the page sideways', async ({ page }) => {
    // Stubbed rather than seeded: the rows are written by the configure phase's
    // callback plugin running in an execution environment, which this stack has
    // no runner for. The LAYOUT is what is under test, and the shape of the
    // payload is pinned by the API contract and by host-results.spec.ts.
    //
    // Two things make this a real phone-width risk rather than a formality: the
    // desktop render is a SIX-COLUMN numeric table, and a `not-reached` row
    // carries em-dashes where every other row carries digits — so the card list
    // has to be what renders here, and a long host name has to wrap.
    const runId = '55555555-5555-4555-8555-555555555555';
    const wsId = '66666666-6666-4666-8666-666666666666';
    const host = (name: string, status: string, reached: boolean) => ({
      id: `rhr-${name}`,
      type: 'run-host-results',
      attributes: {
        host: name,
        status,
        ok: reached ? 4 : null,
        changed: reached ? 1 : null,
        failures: reached && status === 'failed' ? 1 : reached ? 0 : null,
        unreachable: reached ? 0 : null,
        skipped: reached ? 2 : null,
        rescued: reached ? 0 : null,
        ignored: reached ? 0 : null,
        'saw-unreachable': status === 'unreachable',
        reached,
      },
    });

    await page.route(`**/api/*/runs/${runId}`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({
          data: {
            id: runId,
            type: 'runs',
            attributes: {
              status: 'applied',
              message: 'responsive host results',
              'created-at': '2026-10-23T10:00:00Z',
              'status-timestamps': { 'applied-at': '2026-10-23T10:01:00Z' },
              actions: {
                'is-confirmable': false,
                'is-discardable': false,
                'is-cancelable': false,
                'is-retryable': false,
              },
            },
            relationships: { workspace: { data: { id: wsId, type: 'workspaces' } } },
          },
        }),
      }),
    );
    await page.route(`**/api/*/runs/${runId}/host-results`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({
          data: [
            // A host name long enough to need wrapping rather than widening.
            host('web-eu-west-1a-application-server-0001.internal', 'ok', true),
            host('web2', 'failed', true),
            host('hdead', 'unreachable', true),
            host('db1', 'not-reached', false),
          ],
          meta: {
            pagination: {
              'current-page': 1,
              'page-size': 4,
              'total-count': 4,
              'total-pages': 1,
            },
            // The remainder a retry would cover (#1973). Server-side, so the
            // panel renders it rather than filtering the rows itself.
            outstanding: {
              count: 3,
              hosts: ['db1', 'hdead', 'web2'],
            },
          },
        }),
      }),
    );
    await page.route(`**/api/*/runs/${runId}/host-events*`, (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/vnd.api+json',
        body: JSON.stringify({ data: [], meta: {} }),
      }),
    );

    await page.goto(`/workspaces/${wsId}/runs/${runId}?view=hosts`);

    // The card list is `md:hidden` and the table `hidden md:block`, and BOTH are
    // in the DOM at every width — so an unscoped locator can pass against the
    // hidden table and prove nothing about this viewport.
    const notReached = page.getByText('Not reached', { exact: true }).filter({ visible: true });
    await expect(notReached).toHaveCount(1, { timeout: 10_000 });
    await expect(notReached).toBeVisible();
    // Scoped to the CARD, which is what this test is about at phone width.
    // An unscoped `getByText('hdead', { exact: true })` was unambiguous only
    // while the outstanding-hosts banner printed its names as one joined text
    // node. F16 above makes each name its own button, so the bare locator
    // matches the banner's link as well as the card and fails strict mode ---
    // which this spec's own next assertion already anticipates for the retry
    // BUTTON, for the same reason. Asserting the card is also the stronger
    // statement: "some element says hdead" would pass with the card list
    // absent, which is the one thing a phone-width test is about.
    await expect(
      page.getByRole('listitem').filter({ hasText: 'hdead' }).filter({ visible: true }),
    ).toHaveCount(1);

    // The outstanding-hosts banner and its action (#1973). Scoped to visible
    // for the same reason as above, and matched on the BUTTON rather than on
    // the host list, because the three host names also appear in the rows -- an
    // assertion on those would pass with the banner absent.
    const retry = page
      .getByRole('button', { name: 'Retry these hosts' })
      .filter({ visible: true });
    await expect(retry).toHaveCount(1);
    await expect(retry).toBeVisible();
    // The count comes from `meta.outstanding`, not from counting the rows: four
    // hosts are shown and three are outstanding, so a panel that counted rows
    // would say four here.
    await expect(
      page.getByText('3 hosts still need configuring').filter({ visible: true }),
    ).toBeVisible();

    await expectNoHorizontalPageScroll(page);
  });
});

test.describe('Workspace form sections (#2253)', () => {
  // The Configuration tab was 1,762 lines and 57 controls, 42 of them in a
  // single undivided card, with zero `<details>` in the file. Sectioning it and
  // collapsing the advanced groups is the fix; these are the guards that the
  // fix did not buy density at the cost of fitting on a phone.
  //
  // Phone width is where it would break: a `<details>` and every grid item has
  // an intrinsic minimum width and refuses to shrink below its content without
  // `min-w-0`, which is the single CSS line behind an overflow this repo has
  // already met once on the autodiscovery page's own collapsed group.

  test('the sectioned Configuration tab fits a phone', async ({ page }) => {
    const token = getStoredToken()
    const wsId = await createWorkspace(token, uniqueName('e2e-wsform-sections'))

    await page.goto(`/workspaces/${wsId}?tab=configuration`)

    // The groups are rendered, not just the old flat card. `identity` and
    // `execution` apply to every workspace, so they are the two that must be
    // present on a bare Terraform workspace with nothing configured.
    await expect(page.getByTestId('ws-group-identity')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('ws-group-execution')).toBeVisible()

    await expectNoHorizontalPageScroll(page)
  })

  test('every group on the create form is open, with no interaction needed', async ({ page }) => {
    // This test used to assert the opposite. `diagnostics` was all-`advanced`,
    // so the create form rendered it as a shut `<details>` with a derived
    // summary of what it held, and the assertion was that it started closed and
    // opened on click.
    //
    // #2413 F4 withdrew that: *"sections make the complex forms navigable even
    // without collapsing or hiding anything and I stand by that"*. So the
    // stronger assertion is the one that now holds — the group's own FIELDS are
    // visible with nothing clicked, which is what "nothing is hidden" means on
    // a form, and there is no `<details>` left on the page to open.
    //
    // At phone width, which is where it would break: this is the project that
    // keeps meeting a grid item refusing to shrink below its content without
    // `min-w-0`.
    await page.goto('/workspaces')
    await page.getByRole('button', { name: 'New Workspace' }).click()

    const group = page.getByTestId('ws-group-diagnostics')
    await expect(group).toBeVisible({ timeout: 15_000 })
    // Its fields, not just its heading. A section that renders empty would
    // satisfy a visibility check on the wrapper alone.
    await expect(page.getByTestId('ws-debug-mode')).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    // Nothing on this form collapses, so there is nothing to open. Asserted
    // rather than implied: the `<details>` idiom returning to one group is how
    // it would return to all of them.
    await expect(page.locator('form details')).toHaveCount(0)
  })

  test('dynamic inventory is ONE ordered select with four options (#2413 F19)', async ({
    page,
  }) => {
    // Two adjacent booleans became one ordered control, because their
    // combination is what nobody could reason about: one of the two permits an
    // arbitrary executable from the repository to run in the runner Job. NOT
    // "the only settings that let third-party code run on a runner", which an
    // earlier version of this comment said -- see `lib/dynamic-inventory.ts`.
    //
    // FOUR options, not the finding's three. `scripts` without `plugins` is a
    // real configuration: the runner's `_enabled_plugins` appends the two
    // independently and yields `script,yaml,ini`, so a tri-state would delete a
    // state. Asserting the COUNT is the guard against that -- a later
    // "simplification" to three would pass a check that only looked for the
    // control.
    await page.goto('/workspaces')
    await page.getByRole('button', { name: 'New Workspace' }).click()

    const select = page.getByTestId('ws-dynamic-inventory-mode')
    await expect(select).toBeVisible({ timeout: 15_000 })
    await expect(select.locator('option')).toHaveCount(4)

    // Ordered by what it permits, so the default is first and the most
    // permissive is last. The order IS the control, so it is asserted.
    await expect(select.locator('option').nth(0)).toHaveText('None')
    await expect(select.locator('option').nth(3)).toHaveText(
      'Declared plugins and executable scripts',
    )

    // The second control is GONE rather than merely unused: leaving one behind
    // would let the pair disagree.
    await expect(page.getByTestId('ws-inventory-scripts-enabled')).toHaveCount(0)

    // Rendered but DISABLED until the workspace declares a configure
    // operation (#2413 F20: nothing is hidden by a gate). That is why the
    // select cannot simply be operated here, and asserting both halves is
    // stronger than ticking the box and moving on -- a regression that left it
    // enabled would offer a control the workspace cannot use.
    await expect(select).toBeDisabled()
    await page.getByLabel('Configure', { exact: true }).check()
    await expect(select).toBeEnabled()

    // And it is usable at phone width, which is where a four-option select
    // with a long last label would push a page sideways.
    await select.selectOption('both')
    await expect(select).toHaveValue('both')
    await expectNoHorizontalPageScroll(page)
  })


  test('the create form offers the sections too, and fits a phone', async ({ page }) => {
    // The headline of #2253: create offered 13 controls against ~50 accepted
    // attributes, so an operator could not set Operations, Ansible, drift, AI
    // summary, security scan or the VCS workflow until after the workspace
    // existed. Both forms now render the same groups, which is what makes the
    // parity gate checkable — this asserts the groups actually reach the DOM at
    // phone width rather than only type-checking.
    await page.goto('/workspaces')

    await page.getByRole('button', { name: 'New Workspace' }).click()

    await expect(page.getByTestId('ws-group-identity')).toBeVisible({ timeout: 15_000 })
    // A group that did NOT exist on this form before: operations is the whole
    // plan/apply-versus-configure declaration.
    await expect(page.getByTestId('ws-group-operations')).toBeVisible()

    await expectNoHorizontalPageScroll(page)
  })

  test('the caches page carries its two new sections at phone width (#2474)', async ({ page }) => {
    // The page gained a package-cache and a VCS-archive-cache section, each
    // with a filter row, a purge panel and a table. Four stacked sections of
    // controls is exactly the shape that overflows at 412px, and the archive
    // table's widest cell is a repository path that does not wrap on its own.
    await page.goto('/admin/binary-cache')
    await expect(page.getByTestId('package-cache-section')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('vcs-archive-cache-section')).toBeVisible()
    await expectNoHorizontalPageScroll(page)

    // The desktop tables are gated `hidden md:block`, so at this width the
    // list reflows to the shared mobile card primitive rather than putting a
    // nested horizontal scroller inside a scrolling page.
    await expect(page.locator('[data-testid="package-cache-section"] table')).toBeHidden()
    await expect(page.locator('[data-testid="vcs-archive-cache-section"] table')).toBeHidden()

    // Both bulk purges are real buttons with a thumb-sized target.
    for (const id of ['package-bulk-purge', 'archive-bulk-purge']) {
      const box = await page.getByTestId(id).boundingBox()
      expect(box, `${id} has no box`).not.toBeNull()
      expect(box!.height, `${id} is ${box!.height}px tall`).toBeGreaterThanOrEqual(44)
    }

    // Irreversible, so it prompts here too — the touch-only tier would be
    // wrong for an action with no undo, and this project is the coarse pointer.
    let msg = ''
    page.once('dialog', async (d) => { msg = d.message(); await d.dismiss() })
    await page.getByTestId('package-bulk-purge').click()
    await expect.poll(() => msg, { timeout: 5_000 }).toMatch(/cannot be undone/i)
    await expectNoHorizontalPageScroll(page)
  })

})
