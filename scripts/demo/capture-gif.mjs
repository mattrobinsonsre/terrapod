/**
 * Capture the eval walkthrough GIF (docs/images/eval-demo.gif).
 *
 * This is a sibling of capture.mjs and exists for the same reason: the previous
 * GIF was made by hand, so nobody re-made it. It went eleven releases without an
 * update while sitting directly under the hero image, showing an older UI than
 * the stills around it.
 *
 * Point it at whichever stack you have. The eval stack is the faithful choice —
 * it is what the README section this illustrates tells you to run — but the dev
 * stack shows the same journey through the same UI. The visible difference is
 * the size of the workspace list; the credentials are not visible either way,
 * since the password field renders as dots.
 *
 * The committed GIF was recorded against the dev stack: `make eval` wedged
 * locally at the time (helm applied nothing for 20 minutes while CI's eval-boot
 * job stayed green), which is tracked separately.
 *
 * Playwright records the journey as video and ffmpeg converts it, rather than
 * stitching screenshots: the browser captures every frame of the real page,
 * including scrolls and transitions, and the result is smooth without any
 * frame-timing code here.
 *
 * Usage — against the eval stack, once `make eval` reports ready:
 *   node scripts/demo/capture-gif.mjs
 * or against the Tilt dev stack:
 *   node scripts/demo/capture-gif.mjs --url https://terrapod.local --password admin
 *
 * Requires ffmpeg.
 */

import { mkdir, rm, readdir } from 'node:fs/promises'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const run = promisify(execFile)

// Borrow the e2e suite's Playwright rather than adding a second copy.
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const { chromium } = createRequire(path.join(repoRoot, 'e2e/package.json'))('playwright')

const arg = (name, fallback) => {
  const i = process.argv.indexOf(`--${name}`)
  return i > -1 ? process.argv[i + 1] : fallback
}

const URL_BASE = arg('url', 'http://localhost:8080')
const OUT = path.resolve(repoRoot, arg('out', 'docs/images/eval-demo.gif'))
const WORK = path.resolve(repoRoot, '.gif-work')

// The eval profile's own credentials, as scripts/eval.sh prints them.
const EMAIL = arg('email', 'admin')
const PASSWORD = arg('password', 'terrapod')

// 1280 wide, matching capture.mjs. This is not a free choice: below about
// 1200 the nav bar wraps Catalog and Agent Pools onto a second row, which looks
// broken beside the stills — where the same nav sits on one line. A GIF still
// wants no 2x pixel ratio (it doubles the frame area for nothing once quantised
// to 256 colours), and every pixel is paid for in megabytes on the README's
// first screen, so the budget is recovered from frame rate and length instead
// of from width.
const VIEWPORT = { width: 1280, height: 720 }

const BEAT = 900

async function main() {
  await rm(WORK, { recursive: true, force: true })
  await mkdir(WORK, { recursive: true })

  const browser = await chromium.launch()
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    recordVideo: { dir: WORK, size: VIEWPORT },
    colorScheme: 'dark',
    ignoreHTTPSErrors: true,
  })
  const page = await ctx.newPage()

  // Hide the dev badge if this is ever pointed at a dev server.
  await ctx.addInitScript(() => {
    const css = document.createElement('style')
    css.textContent = '[data-next-badge-root],[data-nextjs-toast]{display:none !important}'
    document.documentElement.appendChild(css)
  })

  const beat = (n = 1) => page.waitForTimeout(BEAT * n)

  // Refuse to record while the UI is showing its reconnecting indicator. It is
  // honest — the SSE stream really is down, typically for a minute after an API
  // restart — but a "Reconnecting…" badge in a promotional GIF reads as a broken
  // product rather than as a stack that was bounced thirty seconds ago.
  const settled = async () => {
    const badge = page.getByText(/reconnecting/i)
    for (let i = 0; i < 60 && (await badge.count()) > 0; i++) {
      await page.waitForTimeout(1000)
    }
  }

  // 1. Land on the login screen.
  await page.goto(`${URL_BASE}/login`)
  await page.locator('#email, input[name="email"]').first().waitFor({ timeout: 30_000 })
  await beat()

  // 2. Sign in, typed rather than filled, so the frames show it happening.
  await page.locator('#email, input[name="email"]').first().type(EMAIL, { delay: 90 })
  await page.locator('input[type="password"]').first().type(PASSWORD, { delay: 90 })
  await beat(0.5)
  await page.locator('button[type="submit"]').first().click()
  await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 30_000 })
  await settled()
  await beat(1.5)

  // 3. The workspace list — seeded, so it is a populated screen rather than the
  //    empty state a fresh install would show. Wait for an actual ROW, not for
  //    <main>: <main> is on screen while the list is still a spinner, and a
  //    frame of a spinner is worse than no frame at all.
  await page.goto(`${URL_BASE}/workspaces`)
  await page.locator('main a[href*="/workspaces/"]:visible').first()
    .waitFor({ timeout: 30_000 })
  await beat(1.5)

  // 4. Into a workspace. Wait on its tab strip, and do NOT swallow the failure:
  //    <main> is present while the page is still a spinner, and a swallowed
  //    wait is how the first two attempts ended on a spinning circle.
  await page.locator('main a[href*="/workspaces/"]:visible').first().click()
  // getByRole, not getByText: the tab strip has a mobile <select> twin, and a
  // text match resolves to its hidden <option> — which never becomes visible,
  // so the wait times out on a page that has actually rendered perfectly well.
  await page.getByRole('button', { name: 'Configuration', exact: true })
    .first().waitFor({ timeout: 30_000 })
  await beat(1.5)

  // 5. The Runs tab, so the run history is on screen.
  await page.getByRole('button', { name: 'Runs', exact: true }).first().click()
  await beat(1.5)

  // 6. And into the completed run. Navigated by URL rather than by clicking a
  //    row: the desktop table's rows are not anchors (only the md:hidden card
  //    list has them), so there is no visible link to click at this width, and
  //    a viewer cannot tell the difference in the recording anyway.
  const target = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('terrapod_auth') || '{}')
    const headers = { Accept: 'application/json' }
    if (auth.token) headers.Authorization = `Bearer ${auth.token}`
    const j = async (u) => (await fetch(u, { headers })).json()
    const ws = await j('/api/v2/organizations/default/workspaces?page[size]=100')
    const hero = ws.data.find((w) => w.attributes.name.startsWith('checkout-prod')) || ws.data[0]
    if (!hero) return null
    const runs = await j(`/api/v2/workspaces/${hero.id}/runs?page[size]=20`)
    const done = (runs.data ?? []).find(
      (r) => r.attributes.status === 'planned' && r.attributes['has-cost-estimate']
    ) || runs.data?.[0]
    if (!done) return null
    return `/workspaces/${hero.id.replace(/^ws-/, '')}/runs/${done.id.replace(/^run-/, '')}`
  })
  if (!target) throw new Error('no completed run to show — seed the estate first')

  await page.goto(`${URL_BASE}${target}`)
  await page.getByRole('button', { name: /plan log/i }).first().waitFor({ timeout: 30_000 })
  await beat(2)
  await page.mouse.wheel(0, 260)
  await beat(2)

  await ctx.close() // flushes the video
  await browser.close()

  const webm = (await readdir(WORK)).find((f) => f.endsWith('.webm'))
  if (!webm) throw new Error('Playwright produced no video')
  const src = path.join(WORK, webm)

  // Two-pass palette: one global palette built from the whole clip, then applied
  // with dithering. A single-pass GIF of a dark UI bands badly on the gradients
  // and the syntax colouring.
  const palette = path.join(WORK, 'palette.png')
  const fps = 9
  const TRIM = '0.8' // seconds of blank page before the app paints
  const filters = `fps=${fps},scale=${VIEWPORT.width}:-1:flags=lanczos`
  await run('ffmpeg', ['-y', '-ss', TRIM, '-i', src, '-vf', `${filters},palettegen=stats_mode=diff`, palette])
  await run('ffmpeg', [
    '-y', '-ss', TRIM, '-i', src, '-i', palette,
    '-lavfi', `${filters}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle`,
    '-loop', '0', OUT,
  ])

  await rm(WORK, { recursive: true, force: true })
  console.log(`  + ${path.relative(repoRoot, OUT)}`)
}

main().catch((e) => {
  console.error(String(e.message || e))
  process.exit(1)
})
