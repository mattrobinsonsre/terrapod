// The two caches that had no admin surface, and the three properties of their
// UI that are easy to get wrong and invisible to every other gate (#2474).
//
// All three are claims about the SOURCE rather than about rendered output,
// because each is a property of how a request is made or a flag is read: no
// snapshot of the page can tell a cursor loop from a page-number loop, and a
// `truncated` flag that is parsed and never rendered looks identical to one
// that was never sent.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

const ARCHIVES = read('../src/components/vcs-archive-cache-panel.tsx')
const PACKAGES = read('../src/components/package-cache-panel.tsx')
const EN = JSON.parse(read('../messages/en.json')).adminBinaryCache

/** The source with comments blanked, newlines kept so a line number still
 *  points at the line in the file.
 *
 *  Needed for the ban below and nowhere else: the archive panel's own
 *  docstring names `fetchAllPages` in order to say why it must not be used, so
 *  a bare search finds the sentence that explains the rule and reports it as a
 *  breach of it. */
function code(src: string): string {
  const blank = (m: string) => m.replace(/[^\n]/g, ' ')
  return src.replace(/\/\*[\s\S]*?\*\//g, blank).replace(/\/\/[^\n]*/g, blank)
}

test('the file list is not empty — a floor, so a moved component cannot pass on nothing', () => {
  assert.ok(ARCHIVES.length > 2000, `archive panel looks truncated (${ARCHIVES.length} bytes)`)
  assert.ok(PACKAGES.length > 2000, `package panel looks truncated (${PACKAGES.length} bytes)`)
})

test('the archive list is driven by the key cursor, never by fetchAllPages', () => {
  // The archive response carries `meta.next-cursor` and NOT `meta.pagination`,
  // because a page comes from a storage walk rather than from rows.
  // `fetchAllPages` walks `page[number]`, so pointing it at this endpoint asks
  // for page 2 of something that has no page numbers — for ever.
  assert.ok(
    !/fetchAllPages/.test(code(ARCHIVES)),
    'the archive panel reaches for fetchAllPages, which cannot page a key cursor',
  )
  // ...and the blanker is doing work: the docstring that explains the ban does
  // name the helper, so a matcher over the raw source reports the explanation.
  assert.ok(/fetchAllPages/.test(ARCHIVES), 'the docstring stopped explaining the ban')
  assert.ok(/meta\?\.\['next-cursor'\]/.test(ARCHIVES), 'the next cursor is never read')
  assert.ok(/params\.set\('after', after\)/.test(ARCHIVES), 'the cursor is never sent back as `after`')
})

test('the PACKAGE list does use fetchAllPages — the two endpoints differ on purpose', () => {
  // Asserted so the test above cannot be satisfied by banning the helper
  // everywhere: one of these lists is an ordinary paged table and the other is
  // a bucket walk, and a gate that could not tell them apart would read as
  // "cursors everywhere", which is wrong about the package cache.
  assert.ok(/fetchAllPages<CachedPackageFile>/.test(PACKAGES), 'the package list stopped paging')
})

test('an empty cursor is what ends the walk, and only after a page has arrived', () => {
  // Before the first fetch an empty cursor means "start at the beginning", so
  // "no more pages" cannot be read off the cursor alone — hence `loaded`.
  assert.ok(/const \[loaded, setLoaded\] = useState\(false\)/.test(ARCHIVES))
  assert.ok(/cursor \? \(/.test(ARCHIVES), 'the next-page control is not gated on the cursor')
  assert.ok(/loaded && \(/.test(ARCHIVES), 'the end-of-list line is not gated on a page having arrived')
})

test('meta.truncated is both read and rendered in words', () => {
  // A total that silently omitted whatever lay past the walk's ceiling would
  // understate exactly what an operator came here to find, so the shortfall is
  // stated rather than hinted at.
  assert.ok(/meta\?\.truncated/.test(ARCHIVES), 'truncated is never parsed')
  assert.ok(/\{truncated && \(/.test(ARCHIVES), 'truncated is parsed and never rendered')
  assert.ok(
    /t\('archives\.truncated', \{ limit: scanLimit \}\)/.test(ARCHIVES),
    'the ceiling is not named in the message, so the figure has no scale',
  )
  assert.ok(/\{limit\}/.test(EN.archives.truncated), 'the English copy does not quote the ceiling')
})

test('a truncated walk withholds the figures from the bulk prompt', () => {
  // A floor quoted as a total, in the last prompt before an irreversible
  // action, is the one mistake this panel exists to prevent.
  //
  // Asserted as the PROPERTY rather than as the exact call: this read
  // `/if \(truncated\) return scope/`, which was true until the sealed-node
  // warning wrapped that same return in `withSealedWarning(scope, …)` — a
  // correct change that broke a test about something else, because the anchor
  // described one spelling of the branch instead of what the branch must not do.
  // The next wrapper would have broken it again.
  // Asserted as an ORDER, not as a text window. The previous version matched the
  // `if (truncated) return …` line and checked that line for `confirmFigures` —
  // which a mutation that HOISTS the interpolation above the guard satisfies
  // perfectly, leaving the truncated prompt quoting a floor as a total. That is
  // the single outcome this test exists to prevent, so the assertion has to be
  // about which comes first.
  const body = code(ARCHIVES)
  const atGuard = body.indexOf('if (truncated) return')
  const atFigures = body.indexOf("archives.confirmFigures")
  assert.ok(atGuard > 0, 'the truncated branch no longer returns early')
  assert.ok(
    atFigures > 0,
    'the figures are never quoted, so the untruncated prompt says less than it can',
  )
  assert.ok(
    atFigures > atGuard,
    'the figures are built before the truncated guard returns, so a floor is quoted as a total',
  )
})

test('a 404 from the package totals is read as "switched off", before any throw', () => {
  // The package cache is an optional capability and its admin routes go with it
  // (#2474), so the 404 is a normal state rather than a failure. The ORDER is
  // the property: both throws must come after the 404 branch, or an operator
  // with the cache off gets a red error banner on a page where everything else
  // works.
  const body = code(PACKAGES)
  const at404 = body.indexOf('404')
  assert.ok(at404 > 0, 'the panel does not look at the status, so it cannot tell off from broken')
  // The two rejections sit on OPPOSITE sides of the 404 check, and each side is
  // load-bearing:
  //
  //   totals rejected -> re-thrown FIRST. A rejected promise has no `.status`,
  //                      so there is nothing to compare against 404.
  //   list rejected   -> re-thrown LAST. With the capability off this IS the
  //                      404, so re-throwing it earlier is the error banner the
  //                      whole branch exists to avoid.
  const atTotalsThrow = body.indexOf('throw totalsResult.reason')
  const atListThrow = body.indexOf('throw listResult.reason')
  assert.ok(atTotalsThrow > 0, 'a rejected totals response is swallowed')
  assert.ok(atListThrow > 0, 'a rejected list is swallowed')
  assert.ok(
    atTotalsThrow < at404,
    'the totals rejection is re-thrown after reading a status it cannot have',
  )
  assert.ok(
    atListThrow > at404,
    "the list's 404 is re-thrown as an error before it is read as the capability being off",
  )
  // allSettled rather than all: `all` rejects on the list's 404 before the
  // totals status can be read at all.
  assert.ok(/Promise\.allSettled/.test(body), 'Promise.all cannot distinguish off from broken')
})

test('the switched-off package cache says so in words, in the section that is still there', () => {
  // Rendering nothing is indistinguishable from a broken panel, so the heading
  // stays and carries one sentence. In the catalogue, so it is translated.
  assert.ok(
    /packages\.capabilityOff/.test(PACKAGES),
    'the panel renders no explanation when the surface is absent',
  )
  assert.ok(
    typeof EN.packages.capabilityOff === 'string' && EN.packages.capabilityOff.length > 20,
    'adminBinaryCache.packages.capabilityOff is missing or too short to explain anything',
  )
  // And it says the artifacts are kept, which is the half an operator acts on:
  // turning the capability off is a pause, not a decommission.
  assert.ok(
    /kept|comes back/.test(EN.packages.capabilityOff),
    'the sentence does not say the cached artifacts survive being switched off',
  )
})

test('a narrowed purge sends the scope it promised in the confirmation', () => {
  // The prompt and the request body read the panel's ecosystem state
  // SEPARATELY, so nothing but this ties them together: a request that sent `''`
  // where the prompt said "every npm artifact" would purge the whole cache
  // after a confirmation that named one ecosystem — on an irreversible action,
  // which is the worst place for a promise the request does not keep. The e2e
  // spec asserts the prompt's wording; it cannot see the body.
  const body = code(PACKAGES)
  // Anchored on the EVICT REQUEST, not on the first `ecosystem:` in the file --
  // which is a TypeScript type annotation (`ecosystem: string`) in the row
  // interface and told us nothing.
  const evict = body.match(/admin\/package-cache\/evict[\s\S]{0,420}/)
  assert.ok(evict, 'the bulk evict request is gone from this panel')
  const sent = evict[0].match(/ecosystem:\s*([A-Za-z_$][\w$]*)/)
  assert.ok(sent, 'the evict request no longer sends an ecosystem at all')
  assert.equal(
    sent[1],
    'purgeEcosystem',
    `the purge request sends \`${sent[1]}\` while the confirmation is built from ` +
      '`purgeEcosystem`; the two must be one value',
  )
  // ...and the confirmation is built from that same state.
  assert.ok(
    /const eco = purgeEcosystem/.test(body),
    'the confirmation no longer reads purgeEcosystem, so the two can diverge',
  )
})

test('the two age filters say which clock they mean, and they do not agree', () => {
  // Package age is measured from the LAST ACCESS — evicting an artifact every
  // run pulls, merely for being old, would re-fetch it immediately. An archive
  // is immutable once written and has no access column, so its age is its
  // write time. "Older than" would be wrong about one of them.
  assert.notEqual(EN.packages.bulkDaysLabel, EN.archives.bulkDaysLabel)
  assert.match(EN.packages.bulkDaysLabel, /used/i)
  assert.match(EN.archives.bulkDaysLabel, /written/i)
  assert.match(EN.packages.bulkDaysHint, /last access/i)
  assert.match(EN.archives.bulkDaysHint, /write time/i)
})

test('every purge on these panels confirms in BOTH pointer modes', () => {
  // Each one is irreversible, so it is the both-modes tier of the two-tier
  // policy and never the touch-only one: losing a cache entry on a stray click
  // is a desktop hazard too.
  for (const [name, src] of [['archives', ARCHIVES], ['packages', PACKAGES]] as const) {
    assert.ok(/confirmIrreversible\(/.test(src), `${name} does not confirm`)
    assert.ok(
      !/confirmTouchMutation/.test(code(src)),
      `${name} uses the touch-only tier for something that cannot be undone`,
    )
    // Two purges per panel: one row, one bulk.
    assert.equal(
      (src.match(/confirmIrreversible\(/g) ?? []).length,
      2,
      `${name} should confirm exactly its row purge and its bulk purge`,
    )
  }
})

test('the archive key travels as a query parameter, encoded', () => {
  // It contains slashes, so a path segment would have to encode them and the
  // server refuses anything not under the archive prefix.
  assert.ok(/key=\$\{encodeURIComponent\(a\.key\)\}/.test(ARCHIVES))
})

test('a whole-repo archive renders as a word, not as a hash', () => {
  assert.ok(/hash === 'full'/.test(ARCHIVES))
  assert.ok(/[a-z]/i.test(EN.archives.pathsFull), 'the whole-repo label is not words')
})
