// Warming from the UI, for every artifact kind the cache supports (#2486).
//
// Three of these are claims about the SOURCE rather than about rendered output,
// because each is a property of how a request is made or how a response is
// read, and no snapshot of the page can see any of them: a poll loop that never
// stops looks identical to one that does, and an outcome field that is parsed
// and never rendered looks identical to one that was never sent.
//
// The parser is tested as a function, because `name version [os/arch …]` is the
// one piece of real logic in the panel and getting it wrong sends the wrong
// thing to the server rather than failing visibly.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { parseWarmLine } from '../src/lib/warm-line.ts'

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

const PANEL = read('../src/components/cache-warm-panel.tsx')
const PAGE = read('../src/app/admin/binary-cache/page.tsx')
const EN = JSON.parse(read('../messages/en.json')).adminBinaryCache

/** The source with comments blanked, newlines kept so a line number still
 *  points at the line in the file. Needed because this panel's own comments
 *  quote the things they forbid. */
function code(src: string): string {
  const blank = (m: string) => m.replace(/[^\n]/g, ' ')
  return src.replace(/\/\*[\s\S]*?\*\//g, blank).replace(/\/\/[^\n]*/g, blank)
}

const BODY = code(PANEL)

test('a floor, so a moved or emptied component cannot pass on nothing', () => {
  assert.ok(PANEL.length > 4000, `the warm panel looks truncated (${PANEL.length} bytes)`)
})

// ── the line parser ────────────────────────────────────────────────────────

test('a name alone parses, because an unversioned item stays legal', () => {
  // Legal, and the panel says so separately — the point of #2486 is that it is
  // never the ONLY way to ask, not that it is forbidden.
  assert.deepEqual(parseWarmLine('community.general'), {
    name: 'community.general',
    version: '',
    platforms: [],
  })
})

test('a name and a version parse into both', () => {
  assert.deepEqual(parseWarmLine('community.general 9.2.0'), {
    name: 'community.general',
    version: '9.2.0',
    platforms: [],
  })
})

test('trailing os/arch tokens are platforms, not versions', () => {
  // The shape the binary-cache warm three sections up this page already uses
  // (`tool version [os/arch …]`), rather than a second syntax for the one
  // ecosystem whose artifacts are per-platform.
  assert.deepEqual(parseWarmLine('aws 6.0.0 linux/amd64 linux/arm64'), {
    name: 'aws',
    version: '6.0.0',
    platforms: ['linux/amd64', 'linux/arm64'],
  })
})

test('platforms are recognised even with no version between them', () => {
  // The version is found as the first NON-platform token rather than by
  // position, so a line that names platforms and no version does not silently
  // take `linux/amd64` as its version — which the server would then refuse
  // with a confusing message about a version it was never given.
  assert.deepEqual(parseWarmLine('aws linux/amd64'), {
    name: 'aws',
    version: '',
    platforms: ['linux/amd64'],
  })
})

test('a go module path is a name, not a platform, despite its slashes', () => {
  // The whole reason platforms are detected on the REST of the line rather
  // than on any token containing a slash: every go module name has slashes in
  // it, and `github.com/spf13/cobra` is not a platform.
  assert.deepEqual(parseWarmLine('github.com/spf13/cobra v1.8.1'), {
    name: 'github.com/spf13/cobra',
    version: 'v1.8.1',
    platforms: [],
  })
})

test('blank and whitespace-only lines parse to nothing', () => {
  // So a trailing newline in a pasted list is not submitted as an empty item.
  assert.equal(parseWarmLine(''), null)
  assert.equal(parseWarmLine('   '), null)
  assert.equal(parseWarmLine('\t'), null)
})

test('surrounding whitespace is not part of the name', () => {
  assert.deepEqual(parseWarmLine('  requests   2.32.3  '), {
    name: 'requests',
    version: '2.32.3',
    platforms: [],
  })
})

// ── the properties no rendering can show ───────────────────────────────────

test('the job is polled, and the poll stops when it finishes', () => {
  // A submission returns a job id rather than a result. A panel that submitted
  // and never polled would report nothing at all; one that polled and never
  // stopped would hit the API every two seconds for as long as the page was
  // open.
  assert.ok(BODY.includes('/admin/warm-jobs/'), 'the job is never polled')
  assert.ok(BODY.includes('setInterval'), 'there is no poll')
  assert.match(
    BODY,
    /status === 'finished'[\s\S]{0,200}clearInterval/,
    'the poll does not stop when the job finishes',
  )
  assert.ok(
    /useEffect\(\(\) =>[\s\S]{0,120}clearInterval/.test(BODY),
    'the poll is not cleared when the component unmounts',
  )
})

test('already-cached is rendered as its own outcome, not folded into success', () => {
  // `fetched` 0 against a non-zero `files` is what tells an operator a re-run
  // converged. Warming is idempotent by design, so re-running is the normal way
  // to close the last gaps — and if both read "fetched", the second run reports
  // exactly what the first did.
  assert.match(
    BODY,
    /o\.fetched === 0 && o\.files > 0/,
    'the already-cached case is not distinguished',
  )
  // Both arms, by their i18n key rather than by the bare word: `fetched` is
  // also a field name and a local variable in this file, so a substring search
  // for it passes whether or not anything is rendered.
  assert.match(BODY, /t\('warmAhead\.alreadyCached'/, 'the already-cached arm renders nothing')
  assert.match(BODY, /t\('warmAhead\.fetched'/, 'the fetched arm renders nothing')
})

test('the effective upstream is shown before anything is submitted', () => {
  // Requirement two of #2486 is half visibility: an operator warming from here
  // could not see which upstream a submission would reach, so "it fetched from
  // somewhere" was the whole of the feedback.
  assert.ok(
    BODY.includes('/admin/package-cache/sources'),
    'the sources endpoint is never read, so the upstream cannot be shown',
  )
  assert.ok(BODY.includes('effectiveUpstream'), 'the effective upstream is not computed')
  assert.ok(BODY.includes('upstreamIs'), 'the effective upstream is not rendered')
})

test('a source is chosen by label, and no URL field exists to type one into', () => {
  // The configured upstream is the whole of this proxy's request-forgery
  // surface precisely because it is operator-set. A free-text URL here would
  // undo that for an admin-authenticated caller, so the control is a select
  // over the declared labels and there is nothing to type a host into.
  assert.match(BODY, /value=\{source\}/, 'there is no source control')
  assert.match(
    BODY,
    /Object\.keys\(selected\?\.mirrors \?\? \{\}\)/,
    'the source options do not come from the declared mirrors',
  )
  assert.ok(
    !/id="warm-source"[\s\S]{0,200}<input/.test(BODY),
    'the source is a free-text input rather than a choice among declared labels',
  )
})

test('a sealed node cannot be submitted to from here', () => {
  // The 409 is the server's answer either way; this is so the form does not
  // offer a submission that can only fail. "0 succeeded" on a sealed node looks
  // exactly like a set of missing packages.
  assert.match(BODY, /res\.status === 409/, 'the sealed refusal is not handled')
  assert.match(BODY, /disabled=\{[^}]*sealed/, 'the submit button is live on a sealed node')
})

test('pulumi cannot be submitted without a version, and the form says why', () => {
  // Refused client-side as well as server-side, because the server refuses the
  // whole submission and the operator would otherwise only learn that from a
  // 422 after pasting fifty lines.
  assert.ok(BODY.includes("resolves-latest"), 'the per-ecosystem rule is not read from the server')
  assert.match(BODY, /disabled=\{[^}]*blocked/, 'a blocked submission can still be sent')
  assert.ok(BODY.includes('versionRequired'), 'the reason is never shown')
})

test('the panel is on the caches page', () => {
  // It belongs beside the listing it fills, rather than on a page of its own:
  // the listing says what is missing and this is what closes the gap.
  assert.ok(PAGE.includes('CacheWarmPanel'), 'the warm panel is not rendered')
  assert.ok(PAGE.includes("from '@/components/cache-warm-panel'"), 'the import is missing')
})

// ── i18n ───────────────────────────────────────────────────────────────────

test('every warmAhead key the panel uses exists in the English catalogue', () => {
  // The completeness gate compares locales against `en`; nothing compares `en`
  // against the component, so a typo'd key renders as the key itself.
  const used = new Set(
    [...PANEL.matchAll(/t(?:\.rich)?\('warmAhead\.([a-zA-Z]+)'/g)].map((m) => m[1]),
  )
  assert.ok(used.size >= 15, `only ${used.size} keys matched — the matcher has stopped matching`)
  const missing = [...used].filter((k) => !(k in EN.warmAhead))
  assert.deepEqual(missing, [], `keys used by the panel and absent from en.json: ${missing}`)
})

test('no warmAhead key is defined and never used', () => {
  // A key nothing reads is the flag-nothing-reads shape, in a catalogue that
  // 31 other locales then have to translate.
  const used = new Set(
    [...PANEL.matchAll(/t(?:\.rich)?\('warmAhead\.([a-zA-Z]+)'/g)].map((m) => m[1]),
  )
  const unused = Object.keys(EN.warmAhead).filter((k) => !used.has(k))
  assert.deepEqual(unused, [], `defined in en.json and never rendered: ${unused}`)
})
