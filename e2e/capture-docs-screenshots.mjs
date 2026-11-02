/**
 * Capture the documentation screenshots.
 *
 * The images in `docs/images/` are the first thing anyone sees of Terrapod, and
 * #2337 means they get regenerated whenever the look changes -- so capturing
 * them is a script rather than an afternoon of dragging windows. Run the seed
 * first; it builds the estate these are taken against, because a development
 * instance is full of fixture workspaces whose names say nothing to a reader:
 *
 *   python3 scripts/docs-screenshots/seed_demo_estate.py --url https://terrapod.local
 *   node    scripts/docs-screenshots/capture.mjs --url https://terrapod.local
 *
 * DESKTOP shots are 1440x900, which is what the existing set uses. PHONE shots
 * are Pixel 7 at ~412px -- deliberately the same viewport the `responsive`
 * Playwright project gates on, so a published screenshot cannot show a width
 * that CI does not test. Both capture at deviceScaleFactor 2, because a 1x PNG
 * of a text-dense UI looks soft on every display anyone actually reads it on.
 *
 * It lives in `e2e/` rather than beside the seed in `scripts/` because node
 * resolves an ESM import from the SCRIPT's directory, not the working
 * directory -- a Playwright script anywhere else cannot find
 * @playwright/test however it is invoked, and ESM does not honour NODE_PATH.
 */

import { chromium, devices } from '@playwright/test'
import { mkdir } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import path from 'node:path'

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`)
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback
}

const BASE = arg('url', 'https://terrapod.local').replace(/\/$/, '')
// A development instance uses a locally-trusted certificate, and node's fetch
// has no per-request equivalent of the browser context's ignoreHTTPSErrors.
// This script is pointed at such an instance by hand and at nothing else.
process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0'
const OUT = path.resolve(arg('out', 'docs/images'))
const USER = arg('user', 'admin')
const PASS = arg('pass', 'admin')
const ONLY = arg('only', null)

// Desktop pages. `wait` is a selector that proves the page has actually
// rendered its content -- a screenshot taken on a spinner is worse than none,
// and `networkidle` does not help on a page holding an SSE stream open.
const DESKTOP = [
  ['workspaces', '/workspaces', 'table, [data-testid="ws-card-list"]'],
  ['login', '/login', '#email, input[type="password"]', { anonymous: true }],
  ['labels-browser', '/labels', 'h1'],
  ['registry-modules', '/registry/modules', 'h1'],
  ['registry-providers', '/registry/providers', 'h1'],
  ['admin-users', '/admin/users', 'h1'],
  ['admin-roles', '/admin/roles', 'h1'],
  ['admin-agent-pools', '/admin/agent-pools', 'h1'],
  ['admin-variable-sets', '/admin/variable-sets', 'h1'],
  ['admin-vcs-connections', '/admin/vcs-connections', 'h1'],
  ['admin-autodiscovery', '/admin/autodiscovery', 'h1'],
  ['admin-binary-cache', '/admin/binary-cache', 'h1'],
  ['admin-audit-log', '/admin/audit-log', 'h1'],
  ['api-tokens', '/settings/tokens', 'h1'],
  ['sessions', '/settings/sessions', 'h1'],
]

// The phone set. Two shots, chosen because they are the two the claim rests on:
// the list, where a desktop table becomes a card list, and a run overview,
// where the tab bar becomes a picker and six insight cards stack into a grid
// you can actually read and act on.
const PHONE = [
  ['mobile-workspaces', '/workspaces', 'h1'],
  ['mobile-run-overview', null, 'h1'],   // resolved at runtime: newest finished run
]

async function signIn(page) {
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' })
  if (!page.url().includes('/login')) return
  // A generous wait, because against a `next dev` instance the FIRST hit to a
  // route compiles it on demand and that alone can exceed Playwright's 30s
  // default -- which surfaces as "waiting for locator" on a form that is
  // perfectly present a moment later, i.e. as a selector bug rather than as the
  // cold start it is. The form is also behind an auth-providers fetch.
  await page.waitForSelector('#email', { timeout: 120_000 })
  await page.fill('#email', USER)
  await page.fill('#password', PASS)
  await page.click('button[type="submit"]')
  // Wait for a thing that only exists once signed in, not for a navigation:
  // the post-login move is a client-side router push, so `waitForURL`'s default
  // `load` state never fires and it times out on a login that worked.
  await page.waitForSelector('nav a[href="/workspaces"], nav', { timeout: 60_000 })
}

/**
 * Find a run that has reached a terminal state, so the overview cards are
 * populated rather than mid-flight.
 *
 * Done from NODE with the `tofu login` token rather than from the page: a bare
 * `fetch` inside the browser answers 401 even in a signed-in session, because
 * the app's own calls carry auth that an ad-hoc fetch does not. Borrowing the
 * token the seed script already uses sidesteps the question entirely.
 */
async function newestFinishedRun() {
  const tok = apiToken()
  if (!tok) return null
  const h = { Accept: 'application/vnd.api+json', Authorization: `Bearer ${tok}` }
  const get = async (p) => {
    const r = await fetch(`${BASE}${p}`, { headers: h })
    return r.ok ? r.json() : null
  }
  const ws = await get('/api/v1/workspaces?page%5Bsize%5D=0')
  // Prefer the SEEDED estate. Taking whatever terminal run happens to be
  // newest put a fixture workspace -- `cli-1988-160729` -- into a published
  // screenshot, which is the sort of drift nobody notices until a reader does.
  const seeded = (ws?.data ?? []).filter((w) => SEEDED_NAMES.has(w.attributes.name))
  const rest = (ws?.data ?? []).filter((w) => !SEEDED_NAMES.has(w.attributes.name))
  for (const w of [...seeded, ...rest]) {
    const rs = await get(`/api/v1/workspaces/${w.id}/runs?page%5Bsize%5D=5`)
    for (const r of rs?.data ?? []) {
      if (['applied', 'errored', 'planned'].includes(r.attributes.status)) {
        if (!seeded.length || SEEDED_NAMES.has(w.attributes.name)) {
          return `/workspaces/${w.id}/runs/${r.id}`
        }
      }
    }
  }
  return null
}

/** Mirrors `scripts/docs-screenshots/seed_demo_estate.py`'s ESTATE. */
const SEEDED_NAMES = new Set([
  'checkout-prod-eu-west-1', 'checkout-staging-eu-west-1',
  'data-platform-dev-eu-west-1', 'data-platform-prod-eu-west-1',
  'identity-prod-eu-west-1', 'identity-staging-eu-west-1',
  'observability-prod-eu-west-1', 'payments-api-prod-eu-west-1',
  'sandbox-dev-eu-west-1', 'edge-cdn::prod', 'fleet-config-prod-eu-west-1',
])

/** The token `tofu login` wrote, so this script holds no credential of its own. */
function apiToken() {
  try {
    const p = path.join(homedir(), '.terraform.d', 'credentials.tfrc.json')
    const creds = JSON.parse(readFileSync(p, 'utf8')).credentials
    const host = new URL(BASE).host
    return (creds[host] ?? Object.values(creds)[0])?.token ?? null
  } catch {
    return null
  }
}

/**
 * Phrases the app renders when a surface has failed to load. These are not
 * decoration: the first version of this script swallowed a missing `wait`
 * selector and published a `login.png` that was a red "Failed to load
 * providers" box where the form should be -- a broken page, captured and
 * reported as a success. A screenshot is a claim about what the product looks
 * like, so the script has to refuse to make a false one.
 */
const FAILURE_TEXT = [
  'Failed to load', 'Failed to fetch', 'Something went wrong',
  "couldn't load", 'Unable to load', 'Internal server error',
]

/**
 * `/api/v1/auth/*` is rate limited to 10 requests a minute by design -- far
 * tighter than the 100 unauthenticated / 1000 authenticated buckets everything
 * else sits in -- so the two surfaces that read it, the login page and the
 * sessions list, are the ones a repeated capture run exhausts. Measured: after
 * three runs `/auth/providers` reported `x-ratelimit-remaining: 0` and
 * `/auth/sessions` answered 429, which the UI renders as "Failed to load
 * providers" and "Failed to load sessions". Neither is a product defect, and
 * neither should fail a capture -- the bucket refills in under a minute.
 */
async function shoot(page, name, url, wait, attempt = 0) {
  await page.goto(`${BASE}${url}`, { waitUntil: 'domcontentloaded' })
  if (wait) {
    // NOT caught. A selector that never appears means the page did not render
    // what this screenshot is of, which is a reason to fail the run rather
    // than to photograph whatever happened to be on screen.
    try {
      await page.waitForSelector(wait, { timeout: 60_000 })
    } catch (e) {
      if (attempt < 2) {
        console.log(`  ${name}: content never rendered -- waiting 65s for the `
          + `auth rate-limit window and retrying`)
        await page.waitForTimeout(65_000)
        return shoot(page, name, url, wait, attempt + 1)
      }
      throw e
    }
  }
  // Hide the dev server's own furniture. `next dev` renders a floating
  // devtools badge into a `<nextjs-portal>` custom element, and it sat in the
  // bottom-left corner of EVERY image in the first run of this script -- a
  // development artefact published as if it were part of the product.
  await page.addStyleTag({
    content: 'nextjs-portal, next-route-announcer { display: none !important }',
  })
  // Settle: fonts decided, any entry transition done. Without this the first
  // paint can be captured mid-transition and the image looks washed out.
  await page.evaluate(() => document.fonts.ready)
  await page.waitForTimeout(600)

  // Assert it worked. If Next renames that element the rule silently stops
  // matching, and a silent stop here means the badge is back in every
  // screenshot with nothing to say so.
  const devFurniture = await page.evaluate(() => {
    const hits = []
    for (const el of document.querySelectorAll('nextjs-portal, next-route-announcer, [data-nextjs-dev-tools-button]')) {
      const r = el.getBoundingClientRect()
      if (r.width > 0 && r.height > 0) hits.push(el.tagName.toLowerCase())
    }
    return hits
  })
  if (devFurniture.length) {
    console.error(`  FAILED ${name}: the dev-server overlay is still visible `
      + `(${devFurniture.join(', ')}) -- it would be published in the image`)
    process.exitCode = 1
    return
  }

  // Transient state is not failure, but it must not be published either: the
  // workspace list holds an SSE stream open and shows "Reconnecting..." while
  // it re-establishes, and that was captured next to the New Workspace button
  // in the headline image. Wait it out rather than failing.
  for (let i = 0; i < 12; i++) {
    const transient = await page.evaluate(() => {
      const t = document.body.innerText || ''
      return ['Reconnecting', 'Connecting', 'Loading…', 'Loading...']
        .filter((p) => t.includes(p))
    })
    if (!transient.length) break
    if (i === 11) {
      console.error(`  FAILED ${name}: still showing ${JSON.stringify(transient)} `
        + `after 12s -- a transient indicator would be published in the image`)
      process.exitCode = 1
      return
    }
    await page.waitForTimeout(1000)
  }

  // Second net, because a page can render its shell and still have a failed
  // panel inside it -- which the `wait` selector above would happily pass.
  const bad = await page.evaluate((phrases) => {
    const hits = []
    for (const el of document.querySelectorAll('body *')) {
      if (el.children.length) continue                  // leaf nodes only
      const t = (el.textContent || '').trim()
      if (!t || t.length > 200) continue
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) continue     // not visible
      if (phrases.some((p) => t.toLowerCase().includes(p.toLowerCase()))) hits.push(t)
    }
    return hits
  }, FAILURE_TEXT)
  if (bad.length) {
    if (attempt < 2) {
      console.log(`  ${name}: ${JSON.stringify(bad[0])} -- waiting 65s for the `
        + `auth rate-limit window and retrying`)
      await page.waitForTimeout(65_000)
      return shoot(page, name, url, wait, attempt + 1)
    }
    console.error(`  FAILED ${name}: still showing an error after `
      + `${attempt + 1} attempts -- ${JSON.stringify(bad.slice(0, 3))}`)
    process.exitCode = 1
    return
  }

  const file = path.join(OUT, `${name}.png`)
  await page.screenshot({ path: file })
  console.log(`  ${name.padEnd(26)} ${url}`)
}

const main = async () => {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch()

  // Signed out, FIRST: the login page reads `/auth/providers`, and signing in
  // for everything below is what spends that 10-a-minute bucket. Taken last it
  // reliably photographs a red error box instead of a form.
  {
    const anon = await browser.newContext({
      viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, ignoreHTTPSErrors: true,
    })
    const p0 = await anon.newPage()
    console.log('signed out')
    for (const [name, url, wait, opts] of DESKTOP) {
      if (!opts?.anonymous) continue
      if (ONLY && !name.includes(ONLY)) continue
      await shoot(p0, name, url, wait)
    }
    await anon.close()
  }

  // Desktop
  const desk = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    ignoreHTTPSErrors: true,
  })
  let page = await desk.newPage()
  await signIn(page)
  console.log('desktop 1440x900 @2x')
  for (const [name, url, wait, opts] of DESKTOP) {
    if (ONLY && !name.includes(ONLY)) continue
    if (opts?.anonymous) continue        // captured separately, signed out
    await shoot(page, name, url, wait)
  }
  const runPath = await newestFinishedRun()
  const session = await desk.storageState()
  await desk.close()

  // Phone -- the exact viewport the `responsive` project gates on.
  const phone = await browser.newContext({
    ...devices['Pixel 7'],
    deviceScaleFactor: 2,
    ignoreHTTPSErrors: true,
    storageState: session,
  })
  page = await phone.newPage()
  console.log(`phone ${devices['Pixel 7'].viewport.width}px @2x (the width CI gates on)`)
  for (const [name, url, wait] of PHONE) {
    if (ONLY && !name.includes(ONLY)) continue
    const target = url ?? runPath
    if (!target) {
      console.error(`  FAILED ${name}: found no applied/errored/planned run. `
        + `Either the instance has none, or the lookup broke -- it is not a reason `
        + `to publish the set without it.`)
      process.exitCode = 1
      continue
    }
    await shoot(page, name, target, wait)
    // The claim these images make is that nothing overflows at this width, so
    // assert it rather than publishing a picture of a page that scrolls.
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    if (overflow > 0) {
      console.error(`  FAIL ${name}: ${overflow}px of horizontal page scroll at phone width`)
      process.exitCode = 1
    }
  }
  await phone.close()

  await browser.close()
}

main().catch((e) => { console.error(e); process.exit(1) })
