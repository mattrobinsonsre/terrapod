// The operator-upload form's coordinate fields, held against the server (#2599).
//
// This is the one property of that form no other gate can see, and the failure
// is silent in the worst way: a field posted under the wrong NAME is a 422 at
// best, and at worst stores a vetted artifact at a cache key no install ever
// asks for — reporting success the whole time. The server reads these names in
// each ecosystem's own `upload_coordinates`, so they are a contract rather than
// a label, and the form's SHAPES table is the only place the browser states
// them.
//
// Claims about the SOURCE and the committed route contract rather than about
// rendered output, for the same reason the sibling cache tests are: no snapshot
// of the page can tell `project` from `module`.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

const PANEL = read('../src/components/package-upload-panel.tsx')
const EN = JSON.parse(read('../messages/en.json')).adminBinaryCache
/** The server's own committed list of every route it serves. */
const ROUTES: string[] = JSON.parse(read('../../services/tests/api/api_route_contract.json'))

/** The ecosystems the form offers, parsed out of SHAPES rather than listed here
 *  — a list would be a second copy of the thing under test. */
function shapes(): { ecosystem: string; artifact: string[]; index: string[] | null }[] {
  const out: { ecosystem: string; artifact: string[]; index: string[] | null }[] = []
  // Each entry runs from `ecosystem: '...'` to the next one (or the end of the
  // array), so the field lists are read per entry rather than file-wide.
  const body = PANEL.slice(PANEL.indexOf('export const SHAPES'))
  const entries = body.split(/ecosystem:\s*'/).slice(1)
  for (const chunk of entries) {
    const ecosystem = chunk.slice(0, chunk.indexOf("'"))
    const artifactAt = chunk.indexOf('artifact:')
    const indexAt = chunk.indexOf('index:')
    assert.ok(artifactAt >= 0 && indexAt > artifactAt, `${ecosystem}: unreadable SHAPES entry`)
    const names = (segment: string) => [...segment.matchAll(/name:\s*'([^']+)'/g)].map((m) => m[1])
    const indexSegment = chunk.slice(indexAt)
    out.push({
      ecosystem,
      artifact: names(chunk.slice(artifactAt, indexAt)),
      index: /index:\s*null/.test(indexSegment) ? null : names(indexSegment),
    })
  }
  return out
}

const SHAPES = shapes()

test('SHAPES is read, and covers every ecosystem the cache serves', () => {
  // The floor: a parser that stopped matching would make every assertion below
  // pass on nothing, which is the way these gates die.
  assert.equal(SHAPES.length, 6, `parsed ${SHAPES.length} shapes, expected 6`)
  assert.deepEqual(
    SHAPES.map((s) => s.ecosystem).sort(),
    ['galaxy', 'go', 'npm', 'nuget', 'pulumi', 'pypi'],
    'the form offers a different set of ecosystems than the cache serves',
  )
  for (const s of SHAPES) {
    assert.ok(s.artifact.length > 0, `${s.ecosystem}: no artifact coordinate fields parsed`)
  }
})

test('every route the form posts to is one the server actually serves', () => {
  // The form builds its path from the ecosystem and the kind, so this is the
  // assertion that a typo in either is caught here rather than as a 404 an
  // operator meets with a file already selected.
  for (const s of SHAPES) {
    const artifact = `POST /api/v1/admin/package-cache/${s.ecosystem}/artifacts`
    assert.ok(ROUTES.includes(artifact), `server does not serve ${artifact}`)
    if (s.index !== null) {
      const index = `POST /api/v1/admin/package-cache/${s.ecosystem}/index`
      assert.ok(ROUTES.includes(index), `form offers an index upload the server does not serve: ${index}`)
    }
  }
})

test('the form offers an index upload exactly where the server has one', () => {
  // BOTH directions. Offering one the server lacks is a 404; omitting one it has
  // leaves a vetted artifact unresolvable on a sealed node, which is the defect
  // the index routes exist to fix — and that direction is the silent one.
  const served = new Set(
    ROUTES.filter((r) => /^POST \/api\/v1\/admin\/package-cache\/[a-z]+\/index$/.test(r))
      .map((r) => r.split('/')[5]),
  )
  const offered = new Set(SHAPES.filter((s) => s.index !== null).map((s) => s.ecosystem))
  assert.deepEqual([...offered].sort(), [...served].sort())
  // Named explicitly as well, because the derivation above would also pass if
  // both sides became empty together.
  assert.deepEqual([...served].sort(), ['galaxy', 'go', 'npm', 'nuget'])
})

test('pypi and pulumi deliberately offer no index upload', () => {
  // Not an omission: PyPI builds its sealed index from the rows it holds, so an
  // upload is advertised the moment it exists, and Pulumi plugins are addressed
  // by filename with no index at all. Pinned so neither is "fixed" by adding a
  // control that would post to a route that does not exist.
  for (const name of ['pypi', 'pulumi']) {
    const shape = SHAPES.find((s) => s.ecosystem === name)
    assert.ok(shape, `${name} missing from SHAPES`)
    assert.equal(shape!.index, null, `${name} should offer no index upload`)
  }
})

test('every coordinate field has a label, and every hint has a hint string', () => {
  // A missing key renders the dotted path to the operator in production, and
  // next-intl's fallback hides it in development — so this is the only place it
  // is caught.
  const fields = EN.upload?.fields ?? {}
  const hints = EN.upload?.hints ?? {}
  for (const s of SHAPES) {
    for (const name of [...s.artifact, ...(s.index ?? [])]) {
      assert.ok(fields[name], `upload.fields.${name} is missing from en.json`)
    }
  }
  // And the converse: a label for a field no shape uses is dead weight that
  // every one of 32 locales is required to translate.
  const used = new Set(SHAPES.flatMap((s) => [...s.artifact, ...(s.index ?? [])]))
  for (const name of Object.keys(fields)) {
    assert.ok(used.has(name), `upload.fields.${name} is translated but no shape uses it`)
  }
  for (const name of Object.keys(hints)) {
    assert.ok(used.has(name), `upload.hints.${name} is translated but no shape uses it`)
  }
})

test('the form never sets Content-Type by hand', () => {
  // The browser sets it WITH the multipart boundary. Setting it by hand omits
  // the boundary and makes the body unparseable upstream — a 422 whose message
  // is about the form rather than about the header that caused it.
  const code = PANEL.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.ok(
    !/['"]Content-Type['"]/i.test(code),
    'the upload sets Content-Type explicitly, which drops the multipart boundary',
  )
})

test('a replacement is confirmed before it is sent', () => {
  // An upload over existing coordinates destroys bytes a run may already depend
  // on, and on a sealed node nothing can fetch them back. The server refuses
  // without `replace`, so this guards the deliberate case rather than standing
  // in for that check — but it must be the IRREVERSIBLE guard, which prompts in
  // both pointer modes, not the touch-only one.
  const code = PANEL.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.ok(/confirmIrreversible\(/.test(code), 'no irreversible confirmation on the upload path')
  assert.ok(
    !/confirmTouchMutation\(/.test(code),
    'a replacement is irreversible, so the touch-only guard is the wrong tier',
  )
})

test('the posted route and the rendered fields come from ONE resolved kind', () => {
  // The defect this prevents: falling back to the artifact FIELDS when an
  // ecosystem has no index document while still building the `/index` PATH from
  // the raw state, which posts artifact coordinates to a route that does not
  // exist. The UI makes that combination unreachable by resetting the mode, so
  // no behavioural test can see it -- the guard has to be that both read the
  // same value.
  const code = PANEL.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
  assert.ok(/const effectiveKind/.test(code), 'no single resolved kind')
  // The path must be built from the resolved value, never from the raw state.
  const pathLine = code.split('\n').find((l) => l.includes("? 'index' : 'artifacts'"))
  assert.ok(pathLine, 'could not find the line that builds the upload path')
  assert.ok(
    /effectiveKind/.test(pathLine!),
    `the upload path is built from the raw state rather than the resolved kind: ${pathLine!.trim()}`,
  )
})

test('the four Origin labels the docs quote are the ones the page renders', () => {
  // `docs/package-cache.md` names these four states in italics so an operator
  // can match what the page shows against what the page MEANS. A doc that
  // quotes a UI string goes stale silently -- the page changes, the sentence
  // does not, and the reader searches for text nothing produces. So the quoted
  // strings are pinned here, which is the only thing coupling them.
  const DOC = readFileSync(
    fileURLToPath(new URL('../../docs/package-cache.md', import.meta.url)),
    'utf8',
  )
  const attestation = EN.attestation ?? {}
  const quoted = {
    upstreamDigest: 'Verified',
    nonePublished: 'No digest published',
    operatorUpload: 'Uploaded',
    unrecorded: 'Not recorded',
  }
  for (const [key, label] of Object.entries(quoted)) {
    assert.equal(
      attestation[key],
      label,
      `en.json renders ${key} as ${attestation[key]}, but the docs quote "${label}"`,
    )
    assert.ok(
      DOC.includes(`*${label}*`),
      `docs/package-cache.md no longer quotes "${label}" in italics`,
    )
  }
  // The column header too, which the docs name as `Origin`.
  assert.equal(EN.columns?.origin, 'Origin')
  assert.ok(/`Origin` column/.test(DOC), 'the docs no longer name the Origin column')
})
