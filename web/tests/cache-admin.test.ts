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

test('the PACKAGE list takes one page at a time, and never walks every page', () => {
  // This used to assert the OPPOSITE -- that the package panel calls
  // `fetchAllPages` -- which was right until the listing's pagination moved
  // into SQL. `fetchAllPages` walks `page[number]=1..n` until the pagination
  // meta says it is done, so at 60,000 cached artifacts that was ~600 requests,
  // restarted by every keystroke in the debounced name filter (#2492).
  //
  // Both halves matter. Banning the helper alone would be satisfied by a panel
  // that fetched nothing; requiring the page params alone would be satisfied by
  // one that sent them and then looped anyway.
  const body = code(PACKAGES)
  assert.ok(
    !/fetchAllPages/.test(body),
    'the package panel walks every page again, which is #2492',
  )
  assert.ok(
    /'page\[size\]': String\(PAGE_SIZE\)/.test(body),
    'the panel does not ask for a page, so the server returns the whole table',
  )
  assert.ok(
    /'page\[number\]'/.test(body),
    'the panel never asks for a page NUMBER, so Load more cannot advance',
  )
  // The archive panel's ban stays a ban, so the two are still told apart: one
  // of these lists is a paged table and the other is a bucket walk.
  assert.ok(!/fetchAllPages/.test(code(ARCHIVES)), 'the archive panel reaches for fetchAllPages')
})

test('the package count line reads the SERVER total, not the rows it rendered', () => {
  // `total-count` is computed over the whole match, so "showing 100 of 60,000"
  // is honest where a count of the rendered rows would read as the whole cache
  // -- and the sortable headers above the table only order what is loaded, so
  // the scope of the sort has to be visible rather than inferred.
  const body = code(PACKAGES)
  // EVERY site that sets the total, not just one of them. The panel sets it in
  // two places -- the first page and each Load more -- and a presence check is
  // satisfied by whichever of the two still reads the meta, leaving the other
  // counting its own rendered rows. A mutation of exactly that shape survived
  // the first version of this assertion.
  const setters = body.match(/setTotal\([^\n]*/g) ?? []
  assert.ok(setters.length >= 2, `expected both total writers, found ${setters.length}`)
  for (const call of setters) {
    assert.ok(
      call.includes("pagination?.['total-count']"),
      `a total is set from something other than the server count: ${call}`,
    )
  }
  assert.ok(
    /entries\.length < total/.test(body),
    'the Load more control is not gated on there being more to load',
  )
  assert.ok(EN.packages.showing.includes('{shown}'), 'the count line does not say how many are shown')
  assert.ok(EN.packages.showing.includes('{total}'), 'the count line does not say of how many')
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
  // SCOPED TO `load`, which is the function this ordering is about. `fetchPage`
  // beside it checks 404 too -- correctly, so a Load more with the capability
  // switched off mid-session is silent rather than a banner -- and a file-wide
  // indexOf would find THAT one and compare the throws against a 404 in another
  // function entirely.
  const whole = code(PACKAGES)
  const from = whole.indexOf('const load = useCallback')
  const to = whole.indexOf('useEffect(')
  assert.ok(from > 0 && to > from, 'the load callback could not be located')
  const body = whole.slice(from, to)
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

// ── the bounded evictions (#2491, #2492) ───────────────────────────────────

test('both panels distinguish a BOUNDED purge from a finished one', () => {
  // The defect (#2491): an eviction removed a bounded slice, reported only a
  // count, and the operator reloaded the totals, saw the same size, and read it
  // as the purge having done nothing. The message is the only place a panel can
  // say otherwise.
  for (const [name, body, key] of [
    ['packages', code(PACKAGES), 'packages'],
    ['archives', code(ARCHIVES), 'archives'],
  ] as const) {
    assert.ok(
      /body\?\.truncated === true/.test(body),
      `the ${name} panel never reads \`truncated\`, so a bounded purge reads as a complete one`,
    )
    assert.ok(
      new RegExp(`${key}\\.successBulkTruncated`).test(body),
      `the ${name} panel has no separate message for a bounded purge`,
    )
    assert.ok(
      EN[key].successBulkTruncated !== EN[key].successBulk,
      `the ${key} truncated message is the same sentence as the complete one`,
    )
  }
})

test('the ARCHIVE purge carries a cursor, because "run it again" is not true for it', () => {
  // The asymmetry, pinned rather than tidied. The package eviction deletes the
  // rows it selected, so repeating the call resumes by itself and the same
  // button does it. The archive eviction walks keys that are mostly NOT
  // deleted, so without the cursor the window restarts at the same place and
  // the rest of the estate is unreachable however many times Purge is pressed.
  const body = code(ARCHIVES)
  assert.ok(
    /\.\.\.\(after \? \{ after \} : \{\}\)/.test(body),
    'the resume cursor never reaches the eviction request',
  )
  assert.ok(
    /body\?\.\['next-cursor'\]/.test(body),
    'the eviction response cursor is never read, so nothing can resume the sweep',
  )
  assert.ok(
    /data-testid="archive-continue-purge"/.test(body),
    'there is no Continue control, so the operator is told more remains and cannot reach it',
  )
  assert.ok(
    /\{purgeCursor && \(/.test(body),
    'the Continue control is not gated on the cursor, so it offers to continue a finished sweep',
  )
  // And the PACKAGE panel must NOT grow one: there is no cursor on that
  // response, so a control driving one would send `after: ""` for ever.
  assert.ok(
    !/next-cursor/.test(code(PACKAGES)),
    'the package panel reads a cursor the package eviction never returns',
  )
})

test('a continued archive sweep re-sends EVERY filter it STARTED with', () => {
  // Continuing with whatever the inputs hold now would make the second window a
  // different question from the first -- an operator who edits a field between
  // windows would purge two different sets and be told it was one.
  //
  // BOTH filters, which is the half easy to get half-right: an earlier version
  // of this panel snapshotted the age and read the connection live, so the
  // comment promising "the filters it started with" was false for one of them.
  // Hence the assertion is that the request body reads the SNAPSHOT for every
  // field rather than that some named piece of state exists.
  const body = code(ARCHIVES)
  const from = body.indexOf('async function purgeWindow')
  const to = body.indexOf('async function purgeMatching')
  assert.ok(from > 0 && to > from, 'purgeWindow could not be located')
  const win = body.slice(from, to)
  assert.ok(
    /connection_id: scope\.connectionId/.test(win),
    'the continued window reads the LIVE connection input, not the one it started with',
  )
  assert.ok(
    /scope\.days === null/.test(win),
    'the continued window reads the LIVE age input, not the one it started with',
  )
  assert.ok(
    !/purgeConnectionId|purgeDays/.test(win),
    'purgeWindow still reaches for a live filter input',
  )
  assert.ok(
    /continuePurge\(\) \{\s*await purgeWindow\(purgeCursor, purgedScope\)/.test(body),
    'Continue does not re-send the stored scope',
  )
})
