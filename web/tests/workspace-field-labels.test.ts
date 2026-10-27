// Every shared workspace field has a label that resolves (#2253).
//
// The create form and the Configuration tab render one shared field set, and
// each field's label is a KEY looked up at render time — `LABEL_KEY[attr]`
// against the `workspaceDetail` namespace. Nothing about that is type-checked,
// and the mapping is deliberately NOT a mechanical kebab-to-camel conversion:
// measured against `en.json`, only 29 of the 55 attributes convert cleanly.
// `vcs-repo-url` is `fields.vcsRepository`, `engine-version` is
// `fields.version`, `resource-cpu` is `fields.cpuRequest`, and the settings the
// specialised cards own live in those cards' own sections (`aiSummary.mode`,
// `securityScan.threshold`, `drift.checkInterval`).
//
// So a typo in that map renders a raw dotted key at an operator, on a form that
// is one of the two most-visited surfaces in the product, and no other gate can
// see it: `i18n:check` compares locales against `en` rather than checking that
// anything ASKS for a key, and `i18n:lint` only catches raw JSX literals.
//
// Read as TEXT rather than imported. The table now lives in `src/lib` and IS
// importable, but these assertions are about what the file DECLARES -- a
// mapping entry pointing at the wrong key resolves fine at runtime, so reading
// the declaration is the point rather than a limitation.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const ROOT = join(import.meta.dirname, '..')
const SOURCE = readFileSync(join(ROOT, 'src/lib/workspace-fields.ts'), 'utf8')
const EN = JSON.parse(readFileSync(join(ROOT, 'messages/en.json'), 'utf8')) as Record<
  string,
  unknown
>

/** The attributes the shared table declares. */
function declaredAttrs(): string[] {
  return [...SOURCE.matchAll(/attr:\s*'([a-z][a-z0-9-]*)'/g)].map((m) => m[1])
}

/** The attr → label-key map, as declared. */
function labelKeys(): Map<string, string> {
  const block = SOURCE.match(/export const LABEL_KEY[^=]*=\s*\{([\s\S]*?)\n\}/)
  assert.ok(block, 'LABEL_KEY not found — this gate reads it by shape, so update the matcher')
  const out = new Map<string, string>()
  for (const m of block[1].matchAll(/'?([a-z][a-zA-Z0-9-]*)'?:\s*'([a-zA-Z0-9.]+)'/g)) {
    out.set(m[1], m[2])
  }
  return out
}

/** Resolve a dotted key under `workspaceDetail`, or undefined. */
function resolve(key: string): string | undefined {
  let node: unknown = EN.workspaceDetail
  for (const part of key.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return typeof node === 'string' ? node : undefined
}

test('the field table and its label map both parse', () => {
  // A floor. Without it a changed declaration shape makes every assertion
  // below pass by finding nothing — green and meaningless.
  assert.ok(
    declaredAttrs().length >= 40,
    `only ${declaredAttrs().length} attrs parsed; the matcher has probably stopped ` +
      'matching the table rather than the table having emptied',
  )
  assert.ok(labelKeys().size >= 40, `only ${labelKeys().size} LABEL_KEY entries parsed`)
})

test('every declared field has a label key', () => {
  const keys = labelKeys()
  const missing = declaredAttrs().filter((a) => !keys.has(a))
  assert.deepEqual(
    missing,
    [],
    `attribute(s) ${missing.join(', ')} are in FIELDS but absent from LABEL_KEY, so both ` +
      'forms would render them with no label',
  )
})

test('every label key resolves in en.json', () => {
  const unresolved: string[] = []
  for (const [attr, key] of labelKeys()) {
    if (resolve(key) === undefined) unresolved.push(`${attr} -> workspaceDetail.${key}`)
  }
  assert.deepEqual(
    unresolved,
    [],
    `label key(s) do not resolve, so the form renders the raw key: ${unresolved.join('; ')}`,
  )
})

test('every group has a heading', () => {
  // The group ids are declared in the same file; their headings live under
  // `workspaceDetail.groups`. A group added without a heading renders a raw
  // key as a section title, which is more visible than a missing field label
  // and just as easy to ship.
  const block = SOURCE.match(/export const GROUPS = \[([\s\S]*?)\] as const/)
  assert.ok(block, 'GROUPS not found — this gate reads it by shape')
  const ids = [...block[1].matchAll(/'([a-zA-Z]+)'/g)].map((m) => m[1])
  assert.ok(ids.length >= 8, `only ${ids.length} group ids parsed`)
  const missing = ids.filter((id) => resolve(`groups.${id}`) === undefined)
  assert.deepEqual(missing, [], `group(s) with no heading in en.json: ${missing.join(', ')}`)
})

test('no label requires an ICU argument, which would throw at render', () => {
  // The sharpest version of the near-miss below, and the one that is not a
  // wrong string but a CRASH: `fields.terragruntEnabledVersion` is
  // `'Enabled (v{version})'`, so using it as a label raises
  // MISSING_FORMAT_VALUE and next-intl renders the raw key. It RESOLVES
  // perfectly, so the resolution test above passes it, and a reviewer reading
  // the map sees a plausible key name.
  const offenders: string[] = []
  for (const [attr, key] of labelKeys()) {
    const value = resolve(key)
    if (value && /\{\s*[A-Za-z0-9_]+/.test(value)) {
      offenders.push(`${attr} -> ${key} = ${JSON.stringify(value)}`)
    }
  }
  assert.deepEqual(
    offenders,
    [],
    'label(s) point at an ICU message needing an argument, which throws at render: ' +
      offenders.join('; '),
  )
})

test('no label key points at a hint, placeholder or title by mistake', () => {
  // A near-miss that type-checks, resolves, and renders the wrong string: the
  // catalogue carries `fields.ansibleVersion` next to
  // `fields.ansibleVersionPlaceholder`, `...Hint` and `...Title`, and picking
  // the neighbour gives a label that is a sentence. Nothing else would catch
  // it, because it resolves perfectly.
  const wrong: string[] = []
  for (const [attr, key] of labelKeys()) {
    if (/(Hint|Placeholder|Title|Suffix|Note)$/.test(key)) wrong.push(`${attr} -> ${key}`)
  }
  assert.deepEqual(
    wrong,
    [],
    `label key(s) point at explanatory text rather than a label: ${wrong.join('; ')}`,
  )
})
