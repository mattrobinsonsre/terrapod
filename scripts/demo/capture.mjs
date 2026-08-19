/**
 * Capture the documentation screenshots (#720).
 *
 * The previous refresh committed only PNGs and no tooling, so recapturing was
 * manual work nobody scheduled — which is why the images sat six weeks stale
 * while the UI moved underneath them. This makes it one command.
 *
 * Two things decide whether the output is any good, and both are set here
 * rather than left to whoever runs it:
 *
 *   VIEWPORT — GitHub renders README images at roughly 850px wide regardless
 *   of source size. A 1440px capture is therefore shown at ~59%, taking 13px
 *   UI text down to about 8px, which is unreadable. Capturing narrower keeps
 *   text legible at the size it is actually displayed. deviceScaleFactor 2
 *   then keeps it crisp on high-density screens.
 *
 *   FRAMING — the viewport, not the page and not an element. A full-page
 *   capture of a mostly-empty dark page wastes half the frame on background;
 *   clipping to <main> instead puts the sticky nav on top of the page heading
 *   and leaves whatever dead space trails the last card. The viewport is what
 *   a person actually sees, so it is what gets photographed — and it keeps
 *   every image the same shape, which a README wants.
 *
 * Usage:
 *   node scripts/demo/capture.mjs --host terrapod.local [--out docs/images]
 *
 * Requires the demo estate: scripts/demo/seed.py --host <host>
 */

import { mkdir } from 'node:fs/promises'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

// Playwright is already a dependency of the e2e suite, and this is a Playwright
// script — so borrow that install rather than adding a second copy to the repo
// for the sake of one file. ESM ignores NODE_PATH, hence the explicit resolve.
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')
const { chromium } = createRequire(path.join(repoRoot, 'e2e/package.json'))('playwright')

const args = Object.fromEntries(
  process.argv.slice(2).flatMap((a, i, all) =>
    a.startsWith('--') ? [[a.slice(2), all[i + 1]?.startsWith('--') ? true : all[i + 1]]] : []
  )
)
const HOST = args.host || 'terrapod.local'
const OUT = args.out || 'docs/images'
const BASE = `https://${HOST}`

// Wide enough for the app's own layout to breathe, narrow enough that its text
// survives GitHub's downscale to ~850px.
const VIEWPORT = { width: 1280, height: 900 }

const shots = []
const shot = (name, fn) => shots.push({ name, fn })

// ── The shot list ───────────────────────────────────────────────────
//
// Deliberately short. Best-in-class projects run one to four images, each
// adjacent to the claim it proves; a gallery gets scrolled past and every
// image is a maintenance liability that will silently go stale.

// ── Page settling ───────────────────────────────────────────────────
//
// Not `networkidle`: the run and workspace pages hold an SSE connection open
// for live updates, so the network is never idle and the wait always times
// out. Wait for the page's own content to be on screen instead, then allow a
// short beat for fonts and any late-arriving panel.
async function settle(page, marker) {
  await page.waitForLoadState('domcontentloaded')
  await page.locator(marker).first().waitFor({ state: 'visible', timeout: 30_000 })
  await page.waitForFunction(() => document.fonts.ready.then(() => true), null, { timeout: 10_000 })
  await page.waitForTimeout(1200)
}

shot('run-detail', async (page, ws, run) => {
  // THE HERO. The governance loop in one frame: what changes, what it costs,
  // what policy says, and what the model thinks of the risk — which is the
  // pitch, and something no competitor screenshot in this category shows.
  await page.goto(`${BASE}/workspaces/${ws}/runs/${run}`)
  await settle(page, 'main')
})

shot('run-log', async (page, ws, run) => {
  // The plan output, framed on the REPLACE. The log opens at the top, which is
  // Terrapod's own orchestration logging (fetching the binary, writing the
  // backend override, chdir) — machinery, and a screenshot of it says nothing
  // about what the run decided. The end is better but lands on whatever
  // resource happens to be last.
  //
  // What a reader wants is the line that makes an approval gate worth having:
  // a resource that must be REPLACED, with the immutable attribute that forces
  // it named right underneath.
  await page.goto(`${BASE}/workspaces/${ws}/runs/${run}?view=plan`)
  await settle(page, 'main')

  const found = await page.evaluate(() => {
    // Finding a line in the log is fiddlier than it looks. The whole log is one
    // <pre>, so there is no per-line element to scroll to; and ANSI colouring
    // splits it into ~1100 text nodes, so no SINGLE node contains a phrase like
    // "must be replaced" — a per-node search silently finds nothing and the
    // capture lands on whatever was on screen.
    //
    // So: concatenate the nodes, search the concatenation, then map the global
    // offset back to the node that holds it, wrap one character in a marker and
    // let the browser do the scrolling.
    const pre = document.querySelector('pre')
    if (!pre) return false

    const walker = document.createTreeWalker(pre, NodeFilter.SHOW_TEXT)
    const nodes = []
    let full = ''
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      nodes.push({ node: n, start: full.length })
      full += n.textContent ?? ''
    }

    const at = full.indexOf('must be replaced')
    if (at < 0) return false

    let target = nodes[0]
    for (const entry of nodes) {
      if (entry.start > at) break
      target = entry
    }
    const offset = Math.min(at - target.start, (target.node.textContent ?? '').length - 1)
    if (offset < 0) return false

    const range = document.createRange()
    range.setStart(target.node, offset)
    range.setEnd(target.node, offset + 1)
    const marker = document.createElement('span')
    range.surroundContents(marker)
    // 'start', not 'center': the header is what we searched for, but the
    // forcing attributes underneath are the point — `launch_type "EC2" ->
    // "FARGATE" # forces replacement` is the whole reason this resource is
    // destroyed and rebuilt. Centring leaves that clipped at the bottom edge.
    // Putting the header at the top of the scrollport lets the block below it
    // fill the frame, and needs no pixel offsets guessed against an element
    // whose identity we would otherwise have to work out.
    marker.scrollIntoView({ block: 'start' })
    return true
  })

  if (!found) {
    const end = page.getByRole('button', { name: /^end$/i })
    if (await end.count()) await end.click()
  }
  // Keep the page itself at the top — only the log pane should have moved.
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.waitForTimeout(1200)
})

shot('run-ai-analysis', async (page, ws, run) => {
  // The differentiator, and the most legible thing in the product: a grounded
  // review that cites the deterministic scan and cost rather than inventing
  // them. Reads at README size in a way a force-directed graph does not.
  await page.goto(`${BASE}/workspaces/${ws}/runs/${run}?view=ai`)
  await settle(page, 'main')
  await page.waitForTimeout(1500)
})

shot('workspaces', async (page) => {
  await page.goto(`${BASE}/workspaces`)
  await settle(page, 'main')
})

// ── Driver ──────────────────────────────────────────────────────────

async function login(page) {
  // The UI authenticates with a Redis-backed session, not the API token, so
  // the capture logs in the way a person does.
  await page.goto(`${BASE}/login`)
  await page.fill('#email, input[name="email"], input[type="text"]', 'admin')
  await page.fill('#password, input[name="password"], input[type="password"]', 'admin')
  await page.click('button[type="submit"]')
  await page.waitForURL((u) => !u.pathname.startsWith('/login'), { timeout: 30_000 })
}

async function findHeroRun(page) {
  // Resolve the hero workspace and its most recent run from the UI's own API
  // rather than hardcoding ids that change on every re-seed.
  const res = await page.evaluate(async () => {
    // The UI authenticates its API calls with the bearer token it stashes at
    // login, so borrow the same one rather than fetching unauthenticated.
    const auth = JSON.parse(localStorage.getItem('terrapod_auth') || '{}')
    const headers = { Accept: 'application/json' }
    if (auth.token) headers.Authorization = `Bearer ${auth.token}`
    const j = async (u) => (await fetch(u, { headers })).json()
    const ws = await j('/api/v2/organizations/default/workspaces?page[size]=100')
    const hero = ws.data.find((w) => w.attributes.name.startsWith('checkout-prod'))
    if (!hero) return null
    // The most recent run is not necessarily the one worth photographing — a
    // one-off run queued by hand has no cost estimate and no AI analysis, and
    // the screenshots exist to show those. Prefer the newest COMPLETE run:
    // planned, with changes, priced. Fall back to the newest so the script
    // still produces something on a fresh estate.
    const runs = await j(`/api/v2/workspaces/${hero.id}/runs?page[size]=20`)
    const complete = (runs.data ?? []).find(
      (r) =>
        r.attributes.status === 'planned' &&
        r.attributes['has-changes'] &&
        r.attributes['has-cost-estimate']
    )
    const chosen = complete ?? runs.data?.[0]
    return {
      ws: hero.id.replace(/^ws-/, ''),
      run: chosen?.id.replace(/^run-/, ''),
      complete: Boolean(complete),
    }
  })
  if (!res?.run) {
    throw new Error('No hero run found — run: scripts/demo/seed.py --host ' + HOST)
  }
  return res
}

const main = async () => {
  await mkdir(OUT, { recursive: true })
  // The impact/estate graphs are WebGL. Headless Chromium has no GPU, and
  // without these it cannot create a context at all — three.js throws and the
  // page's error boundary swallows the whole view. SwiftShader renders it on
  // the CPU: slower, pixel-identical for our purposes.
  const browser = await chromium.launch({
    args: [
      '--use-gl=angle',
      '--use-angle=swiftshader',
      '--enable-unsafe-swiftshader',
      '--enable-webgl',
      '--ignore-gpu-blocklist',
    ],
  })
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    ignoreHTTPSErrors: true, // terrapod.local uses a local CA
    colorScheme: 'dark',
  })
  const page = await ctx.newPage()

  await login(page)
  const { ws, run, complete } = await findHeroRun(page)
  console.log(
    `  hero: workspace ${ws} run ${run}` +
      (complete ? '' : '  (WARNING: this run has no cost estimate — re-run seed.py)')
  )

  // The Next.js dev-mode badge is not part of the product and reads as
  // "captured from a hot-reload dev server". Hidden rather than depended on,
  // so the capture works against either build.
  await ctx.addInitScript(() => {
    const css = document.createElement('style')
    css.textContent =
      '[data-next-badge-root],[data-nextjs-toast],#__next-build-watcher{display:none !important}'
    document.documentElement.appendChild(css)
  })

  for (const { name, fn } of shots) {
    await fn(page, ws, run)
    const file = path.join(OUT, `${name}.png`)
    // Full width from the very top, so the nav and its version number are in
    // frame, down to wherever the page's content actually stops — capped at the
    // viewport. A fixed-height viewport shot of a short page (the run overview
    // is about half a screen) spends the rest of the frame on empty
    // background; a long page (the workspace list) simply fills it.
    //
    // page.screenshot with an explicit clip, not locator.screenshot: the latter
    // waits for its target to stop moving, and the impact graph's force
    // simulation never fully does.
    const height = await page.evaluate((vh) => {
      const m = document.querySelector('main')
      if (!m) return vh
      const bottom = m.getBoundingClientRect().bottom + window.scrollY
      return Math.min(Math.max(Math.ceil(bottom) + 24, 320), vh)
    }, VIEWPORT.height)
    await page.screenshot({
      path: file,
      clip: { x: 0, y: 0, width: VIEWPORT.width, height },
    })
    console.log(`  + ${file} (${VIEWPORT.width}x${height})`)
  }

  await browser.close()
}

main().catch((e) => {
  console.error(String(e.message || e))
  process.exit(1)
})
