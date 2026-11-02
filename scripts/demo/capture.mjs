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
 *   VIEWPORT — 1440x900 at deviceScaleFactor 1, so the committed file is
 *   exactly 1440x900 and every desktop image is the same shape. Maintainer's
 *   instruction, 2026-11-02, after a run shipped 2560-wide files whose heights
 *   ranged from 640 to 1800: a set of images at one width and a dozen
 *   different aspect ratios renders badly in a README, where they sit one
 *   above another and the tall ones dominate.
 *
 *   An earlier version of this comment argued for 1280 at 2x on the grounds
 *   that GitHub renders at roughly 850px and a narrower capture survives the
 *   downscale better. That reasoning is not wrong about downscaling and it is
 *   not what governs here: uniformity across the set matters more than the
 *   last few percent of text legibility in any one image, and the phone shots
 *   below are the deliberate exception.
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
const { chromium, devices } = createRequire(path.join(repoRoot, 'e2e/package.json'))('playwright')

const args = Object.fromEntries(
  process.argv.slice(2).flatMap((a, i, all) =>
    a.startsWith('--') ? [[a.slice(2), all[i + 1]?.startsWith('--') ? true : all[i + 1]]] : []
  )
)
const HOST = args.host || 'terrapod.local'
const OUT = args.out || 'docs/images'
const BASE = `https://${HOST}`

// `--only a,b` re-shoots just those names.
//
// A guard refusing ONE page is the normal outcome of a run -- that is what
// the guards are for -- and without this the only way to retry it was to
// redo all 34, which takes minutes and spends the `/api/v1/auth/*` bucket
// (10/min) on logins it does not need. The exit code still reports the
// refusal, so a filtered run is a retry and not a way to hide one.
const ONLY = typeof args.only === 'string' ? new Set(args.only.split(',')) : null
const wanted = (name) => !ONLY || ONLY.has(name)

// Wide enough for the app's own layout to breathe, narrow enough that its text
// survives GitHub's downscale to ~850px.
const VIEWPORT = { width: 1440, height: 900 }

const shots = []
// A separate list because it is a separate browser context, and because the two
// answer different questions: the desktop set shows what the product does, this
// one shows that it does it on a phone.
const phoneShots = []
const phoneShot = (name, fn) => phoneShots.push({ name, fn })
const shot = (name, fn) => shots.push({ name, fn })

//: A page whose whole content is "nothing here yet" teaches a reader nothing,
//: so it is refused -- except where the empty view IS the subject. One shot
//: qualifies: a docs page explaining the runs tab wants the populated view and
//: the empty one side by side, which is why `checkout-staging` is seeded with
//: `run: None`. Anything else showing an EmptyState is a seeding gap.
const EMPTY_STATE_IS_THE_POINT = new Set(['workspace-runs'])

//: Characters of rendered text, counting a same-origin iframe's own document,
//: below which a page rendered nothing at all.
//:
//: A BACKSTOP, not the main check -- the empty-state and blank-iframe tests
//: above are exact, and this only has to catch a page that produced no content
//: whatsoever. It is low on purpose: the sign-in page is a heading and two
//: fields, 71 characters in English, and it is a perfectly good screenshot. A
//: floor set high enough to have an opinion about a page like that produces
//: findings that are mostly wrong, which is how a gate gets ignored.
const SUBSTANCE_FLOOR = 50

/**
 * Refuse to publish a page that is not showing the product.
 *
 * Every check here exists because a capture run shipped the thing it catches.
 * A screenshot is a claim about what Terrapod looks like, so the script has to
 * be able to decline to make a false one -- and the image is the only evidence,
 * so nothing downstream will notice if it does not.
 *
 * Returns false (and sets a non-zero exit) rather than throwing, so one bad
 * page does not cost the whole run.
 */
async function assertClean(page, name) {
  // Transient state is not failure, but it must not be published either. The
  // workspace list holds an SSE stream open and shows "Reconnecting..." while
  // it re-establishes; that was captured beside the New Workspace button.
  //
  // A SPINNER counts too, and it is the one a text check cannot see: the
  // Inventory tab was captured mid-load, with `LoadingSpinner` turning where
  // the hosts should be, because that component renders no text at all. It is
  // matched on `animate-spin`, which is the only thing it has.
  for (let i = 0; i < 12; i++) {
    const busy = await page.evaluate(() => {
      const t = document.body.innerText || ''
      const hits = ['Reconnecting', 'Connecting'].filter((w) => t.includes(w))
      for (const el of document.querySelectorAll('.animate-spin')) {
        const r = el.getBoundingClientRect()
        if (r.width > 0 && r.height > 0) { hits.push('a spinner'); break }
      }
      return hits
    })
    if (!busy.length) break
    if (i === 11) {
      console.error(`  ! ${name}: still ${JSON.stringify(busy)} after 12s`)
      process.exitCode = 1
      return false
    }
    await page.waitForTimeout(1000)
  }

  // A failed panel inside a page whose shell rendered perfectly well. A
  // `login.png` went out as a red "Failed to load providers" box where the form
  // should be, and the same run produced a sessions page reading "Failed to
  // load sessions".
  //
  // NEITHER was a product defect, which is the part worth remembering:
  // `/api/v1/auth/*` is rate limited to 10 requests a minute against 100
  // unauthenticated and 1000 authenticated elsewhere, and repeated capture runs
  // drain it. Hence the retry rather than a hard failure -- the bucket refills
  // in under a minute.
  const FAILURE_TEXT = ['Failed to load', 'Failed to fetch', 'Something went wrong',
                        'Unable to load', 'Internal server error']
  for (let attempt = 0; attempt < 3; attempt++) {
    const bad = await page.evaluate((phrases) => {
      const hits = []
      for (const el of document.querySelectorAll('body *')) {
        if (el.children.length) continue
        const t = (el.textContent || '').trim()
        if (!t || t.length > 200) continue
        const r = el.getBoundingClientRect()
        if (r.width === 0 || r.height === 0) continue
        if (phrases.some((p) => t.toLowerCase().includes(p.toLowerCase()))) hits.push(t)
      }
      return hits
    }, FAILURE_TEXT)
    if (!bad.length) break
    if (attempt === 2) {
      console.error(`  ! ${name}: ${JSON.stringify(bad.slice(0, 2))} after 3 attempts`)
      process.exitCode = 1
      return false
    }
    console.log(`  . ${name}: ${JSON.stringify(bad[0])} -- waiting 65s for the `
      + `auth rate-limit window`)
    await page.waitForTimeout(65_000)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2000)
  }

  // The dev overlay. Hidden HERE, directly, rather than trusting the init
  // script: that script appends a <style> at document-start, where
  // `document.documentElement` can still be null, so the append throws and the
  // rule never lands -- silently, which is the whole problem with this class.
  //
  // And the check is on `display`, NOT on a bounding rect. `<nextjs-portal>`
  // measures 0x0 because its seventeen children are positioned fixed inside
  // its shadow root, so a `width > 0 && height > 0` test passes while the badge
  // is plainly in the corner of the image. It was, in the first Inventory
  // capture.
  const overlay = await page.evaluate(() => {
    const sel = 'nextjs-portal,next-route-announcer,[data-next-badge-root],'
      + '[data-nextjs-toast],[data-nextjs-dev-tools-button],#__next-build-watcher'
    for (const el of document.querySelectorAll(sel)) el.style.setProperty('display', 'none', 'important')
    return [...document.querySelectorAll(sel)]
      .filter((el) => getComputedStyle(el).display !== 'none')
      .map((el) => el.tagName.toLowerCase())
  })
  if (overlay.length) {
    console.error(`  ! ${name}: dev overlay still visible (${overlay.join(', ')})`)
    process.exitCode = 1
    return false
  }

  // Does the page actually SHOW anything?
  //
  // EMPTY, not WRONG -- and the difference is worth knowing before trusting
  // this. A broken <redoc> element spilled the tail of its own theme JSON into
  // the page as text; that cleared the floor below and the frame was not blank,
  // so this passed a page showing nothing but a fragment of its own markup.
  // Looking at the image is what caught it. These checks remove the failures
  // that are invisible to a glance; they do not replace the glance.
  //
  // The checks above all ask "is something WRONG" -- an error, a spinner, the
  // dev badge. None of them asks the prior question, and a page can be
  // perfectly clean and still be worthless as documentation. Two shipped:
  //
  //   admin-vcs-connections  "No VCS connections configured." -- the seed never
  //                          made one, so the image taught a reader nothing
  //   api-docs-swagger       a toolbar above a blank rectangle -- the page is an
  //                          <iframe src="/api/docs"> and the shot was taken
  //                          before the embed rendered
  //
  // They are different failures and both read as success to every other check.
  // The empty state is matched on the COMPONENT's test id rather than on its
  // words, so it holds in all 32 locales; the iframe is measured through its
  // own document, because its text is not in `main.innerText` at all.
  for (let attempt = 0; attempt < 8; attempt++) {
    const c = await page.evaluate(() => {
      const main = document.querySelector('main') || document.body
      const visible = (el) => {
        const r = el.getBoundingClientRect()
        return r.width > 0 && r.height > 0
      }
      const empty = [...main.querySelectorAll('[data-testid="empty-state"]')]
        .filter(visible)
        .map((e) => (e.textContent || '').trim().slice(0, 60))
      let framed = 0
      let blankFrames = 0
      for (const f of document.querySelectorAll('iframe')) {
        if (!visible(f) || f.getBoundingClientRect().height < 80) continue
        // `contentDocument` returns NULL, without throwing, for a frame the
        // page cannot read -- so an earlier version of this caught nothing: the
        // `catch` was unreachable and `if (!body) continue` skipped the one
        // case it existed to find. A frame we cannot read is a frame we cannot
        // vouch for, so it counts as blank. That is what let a blank Swagger UI
        // through this very check.
        let body = null
        try {
          body = f.contentDocument && f.contentDocument.body
        } catch {
          body = null
        }
        if (!body) {
          blankFrames++
          continue
        }
        const n = (body.innerText || '').trim().length
        framed += n
        if (n < 40) blankFrames++
      }
      // `main` falls back to <body> on a page that has none -- /api-docs is one
      // -- and <body> includes the nav, which is ~180 characters of chrome on
      // every page. Counting that is how a page showing nothing but its own
      // navigation clears a floor meant to catch exactly that, so the nav's
      // text is excluded when the fallback is in play.
      const chrome = document.querySelector('main')
        ? 0
        : [...document.querySelectorAll('nav, header')]
            .reduce((n, el) => n + (el.innerText || '').trim().length, 0)
      const len = Math.max(0, (main.innerText || '').trim().length - chrome)
      return { len, framed, blankFrames, empty }
    })

    // An empty state does not become populated by waiting -- it is a seeding
    // gap -- so this refuses at once rather than spending 8 seconds first.
    if (c.empty.length && !EMPTY_STATE_IS_THE_POINT.has(name)) {
      console.error(`  ! ${name}: empty state -- ${JSON.stringify(c.empty[0])}`)
      process.exitCode = 1
      return false
    }

    const substance = c.len + c.framed
    if (!c.blankFrames && substance >= SUBSTANCE_FLOOR) break

    if (attempt === 7) {
      const why = c.blankFrames
        ? `${c.blankFrames} embedded frame(s) still blank`
        : `only ${substance} characters of content`
      console.error(`  ! ${name}: nothing meaningful on the page -- ${why}`)
      process.exitCode = 1
      return false
    }
    await page.waitForTimeout(1000)
  }

  return true
}

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

phoneShot('mobile-workspaces', async (page) => {
  // The desktop table becomes a list of cards -- the clearest single
  // demonstration that the UI adapts rather than merely shrinks.
  await page.goto(`${BASE}/workspaces`)
  await settle(page, 'main')
})

phoneShot('mobile-run-overview', async (page, ws, run) => {
  // A run's whole verdict on a 412px screen: status, then changes, cost, AI
  // analysis, policy, security scan and runner headroom as stacked cards, with
  // the tab bar collapsed to a picker.
  await page.goto(`${BASE}/workspaces/${ws}/runs/${run}`)
  await settle(page, 'main')
})

// ── The documentation set ───────────────────────────────────────────
//
// The four above are the README's, and the comment there about keeping a
// gallery short is about THAT set. These are different: each one sits beside
// its own `docs/*.md` page, so they are referenced individually and a missing
// one is a doc with a broken image rather than a longer gallery. #2337's
// acceptance asks for all 30 Terrapod screenshots, which is what this is.

const anonShots = []
const anonShot = (name, url, marker = 'main') =>
  anonShots.push({
    name,
    fn: async (page) => {
      await page.goto(`${BASE}${url}`)
      await settle(page, marker)
    },
  })

const page_ = (name, url, marker = 'main') =>
  shot(name, async (page) => {
    await page.goto(`${BASE}${url}`)
    await settle(page, marker)
  })

// Signed OUT, and taken FIRST. The signed-in context redirects away from
// /login so the form never appears there; and signing in for everything else
// spends the `/api/v1/auth/*` bucket, which is 10 a minute, so a login shot
// taken last reliably photographs a "Failed to load providers" box instead.
anonShot('login', '/login', 'input[type="password"]')
page_('labels-browser', '/labels')
page_('registry-modules', '/registry/modules')
page_('registry-providers', '/registry/providers')
page_('api-tokens', '/settings/tokens')
page_('sessions', '/settings/sessions')
page_('admin-users', '/admin/users')
page_('admin-roles', '/admin/roles')
page_('admin-agent-pools', '/admin/agent-pools')
page_('admin-variable-sets', '/admin/variable-sets')
page_('admin-vcs-connections', '/admin/vcs-connections')
page_('admin-autodiscovery', '/admin/autodiscovery')
page_('admin-binary-cache', '/admin/binary-cache')
page_('admin-audit-log', '/admin/audit-log')

// The workspace tabs. `?tab=` is the source of truth for which one is shown
// (#722 made the URL the state), so each is a plain navigation.
const wsTab = (name, tab) =>
  shot(name, async (page, ws) => {
    await page.goto(`${BASE}/workspaces/${ws}${tab ? `?tab=${tab}` : ''}`)
    await settle(page, 'main')
  })

wsTab('workspace-overview', 'configuration')
wsTab('workspace-variables', 'variables')
wsTab('workspace-runs-populated', 'runs')
wsTab('workspace-state', 'state')
wsTab('workspace-notifications', 'notifications')
wsTab('workspace-run-tasks', 'run-tasks')
wsTab('workspace-configurations', 'versions')

shot('workspace-runs', async (page) => {
  // The EMPTY state, deliberately a different workspace from
  // `workspace-runs-populated`. A docs page explaining the runs tab wants both,
  // and `checkout-staging` is seeded with `run: None` precisely so one exists.
  const ws = await byName(page, 'checkout-staging-eu-west-1')
  if (!ws) return
  await page.goto(`${BASE}/workspaces/${ws}?tab=runs`)
  await settle(page, 'main')
})

shot('admin-agent-pool-detail', async (page) => {
  const pool = await page.evaluate(async () => {
    const auth = JSON.parse(localStorage.getItem('terrapod_auth') || '{}')
    const headers = { Accept: 'application/json' }
    if (auth.token) headers.Authorization = `Bearer ${auth.token}`
    const r = await fetch('/api/v1/agent-pools', { headers })
    if (!r.ok) return null
    return ((await r.json()).data || [])[0]?.id ?? null
  })
  if (!pool) {
    console.error('  ! no agent pool on this instance')
    process.exitCode = 1
    return
  }
  await page.goto(`${BASE}/admin/agent-pools/${pool}`)
  await settle(page, 'main')
})

// This page has NO <main>: it renders the nav, a toolbar and an iframe, so the
// usual marker never appears and the shot times out. The iframe is the content.
const apiDocs = async (page, swagger) => {
  await page.goto(`${BASE}/api-docs`)
  await settle(page, 'iframe')
  if (swagger) {
    // The view is component state rather than a URL, so it has to be clicked.
    const btn = page.getByRole('button', { name: /swagger/i })
    if (await btn.count()) await btn.first().click()
  }
  // The spec is fetched and rendered INSIDE the frame, so the element being
  // present says nothing about the document having drawn.
  await page.waitForTimeout(3500)
}

shot('api-docs-redoc', (page) => apiDocs(page, false))
shot('api-docs-swagger', (page) => apiDocs(page, true))

shot('impact-graph', async (page, ws, run) => {
  // The plan's blast radius. A force-directed graph, so it needs longer to
  // settle than a table -- and `locator.screenshot` is the wrong tool for it
  // because the simulation never fully stops, which is why the capture below
  // uses an explicit clip.
  await page.goto(`${BASE}/workspaces/${ws}/runs/${run}?view=impact`)
  await settle(page, 'main')
  await page.waitForTimeout(3500)
})

// ── Ansible and Pulumi ──────────────────────────────────────────────
//
// 2.0 leads on both and the documentation could show neither: the estate was
// all Terraform, so there was no configure, no inventory and no stack to
// photograph. These are the surfaces that distinguish them from a plan.

shot('workspace-inventory', async (page) => {
  const ws = await byName(page, 'fleet-config-prod-eu-west-1')
  if (!ws) return
  await page.goto(`${BASE}/workspaces/${ws}?tab=inventory`)
  await settle(page, 'main')
})

shot('workspace-pulumi', async (page) => {
  const ws = await byName(page, 'edge-cdn::prod')
  if (!ws) return
  await page.goto(`${BASE}/workspaces/${ws}`)
  await settle(page, 'main')
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

/** Resolve a workspace id by name, using the token the UI stashed at login. */
async function byName(page, name) {
  const id = await page.evaluate(async (want) => {
    const auth = JSON.parse(localStorage.getItem('terrapod_auth') || '{}')
    const headers = { Accept: 'application/json' }
    if (auth.token) headers.Authorization = `Bearer ${auth.token}`
    const r = await fetch('/api/v1/workspaces?page%5Bsize%5D=0', { headers })
    if (!r.ok) return null
    const j = await r.json()
    return (j.data || []).find((w) => w.attributes.name === want)?.id ?? null
  }, name)
  if (!id) {
    console.error(`  ! workspace ${name} not found -- run seed.py first`)
    process.exitCode = 1
  }
  return id
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
    // 1, not 2: the committed file is then exactly VIEWPORT, which is what
    // makes "every desktop image is 1440x900" checkable by looking at it.
    deviceScaleFactor: 1,
    ignoreHTTPSErrors: true, // terrapod.local uses a local CA
  })
  const page = await ctx.newPage()

  // The signed-out pass, before anything signs in.
  if (anonShots.length) {
    const anonCtx = await browser.newContext({
      viewport: VIEWPORT, deviceScaleFactor: 1, ignoreHTTPSErrors: true,
    })
    const anon = await anonCtx.newPage()
    for (const { name, fn } of anonShots) {
      if (!wanted(name)) continue
      await fn(anon)
      if (!(await assertClean(anon, name))) continue
      const file = path.join(OUT, `${name}.png`)
      await anon.screenshot({ path: file, clip: { x: 0, y: 0, ...VIEWPORT } })
      console.log(`  + ${file} (${VIEWPORT.width}x${VIEWPORT.height})`)
    }
    await anonCtx.close()
  }

  await login(page)
  const { ws, run, complete } = await findHeroRun(page)
  console.log(
    `  hero: workspace ${ws} run ${run}` +
      (complete ? '' : '  (WARNING: this run has no cost estimate — re-run seed.py)')
  )

  // The Next.js dev-mode badge is not part of the product and reads as
  // "captured from a hot-reload dev server". Hidden rather than depended on,
  // so the capture works against either build.
  //
  // `nextjs-portal` is the one that matters now, and the three selectors after
  // it are what this hid originally -- they match NOTHING in current Next. That
  // is the whole reason `assertClean()` below asserts the badge is gone instead
  // of trusting this rule: a guard whose selector silently stops matching puts
  // the badge back into every committed image with nothing to say so, and in a
  // screenshot pipeline the image is the only evidence there is.
  await ctx.addInitScript(() => {
    const css = document.createElement('style')
    css.textContent =
      'nextjs-portal,next-route-announcer,' +
      '[data-next-badge-root],[data-nextjs-toast],#__next-build-watcher' +
      '{display:none !important}'
    document.documentElement.appendChild(css)
  })

  for (const { name, fn } of shots) {
    if (!wanted(name)) continue
    await fn(page, ws, run)
    if (!(await assertClean(page, name))) continue
    const file = path.join(OUT, `${name}.png`)
    // THE VIEWPORT. Not the page, not an element, and not the content height.
    //
    // This clipped to wherever `<main>` stopped for one run, to avoid spending
    // the lower half of a short page's frame on empty background. The cost was
    // not obvious until the whole set was regenerated: 31 images at one width
    // and heights from 640 to 1800, which is how a README full of screenshots
    // comes to look like a pile of offcuts. Uniform shape beats tight cropping
    // every time here, and the empty background it was avoiding is cheap.
    //
    // page.screenshot with an explicit clip, not locator.screenshot: the latter
    // waits for its target to stop moving, and the impact graph's force
    // simulation never fully does.
    await page.screenshot({
      path: file,
      clip: { x: 0, y: 0, ...VIEWPORT },
    })
    console.log(`  + ${file} (${VIEWPORT.width}x${VIEWPORT.height})`)
  }

  // ── The phone pass ──────────────────────────────────────────────────
  //
  // There was no portrait image in `docs/images/` at all, across 37 files, so
  // the mobile-first UI -- a hard requirement, gated in CI -- had no evidence
  // behind it anywhere in the documentation.
  //
  // The viewport is `devices['Pixel 7']` deliberately: that is the exact device
  // the `responsive` Playwright project runs the responsive specs at, so a
  // published screenshot cannot show a width CI does not test.
  //
  // Full height here rather than clipped to `<main>`, because on a phone the
  // page IS the scroll container (#722) -- the frame a reader recognises is the
  // whole screen, and the stacked cards fill it.
  const phoneCtx = await browser.newContext({
    ...devices['Pixel 7'],
    deviceScaleFactor: 2,
    ignoreHTTPSErrors: true,
    storageState: await ctx.storageState(), // sign in once, not twice
  })
  const phone = await phoneCtx.newPage()
  console.log(`  phone ${devices['Pixel 7'].viewport.width}px @2x (the width CI gates on)`)

  for (const { name, fn } of phoneShots) {
    if (!wanted(name)) continue
    await fn(phone, ws, run)
    if (!(await assertClean(phone, name))) continue

    // The claim these images make is that nothing overflows at this width.
    // Assert it rather than publishing a picture of a page that scrolls.
    const overflow = await phone.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    if (overflow > 0) {
      console.error(`  ! ${name}: ${overflow}px of horizontal page scroll at phone width`)
      process.exitCode = 1
      continue
    }

    const file = path.join(OUT, `${name}.png`)
    await phone.screenshot({ path: file })
    const vp = devices['Pixel 7'].viewport
    console.log(`  + ${file} (${vp.width}x${vp.height} @2x)`)
  }
  await phoneCtx.close()

  await browser.close()
}

main().catch((e) => {
  console.error(String(e.message || e))
  process.exit(1)
})
