// The three phase slots a variable can be held in (#2189, #2190).
//
// A key is ordinary (every phase), apply-only (an apply and a configure), or
// configure-only (a configure alone). The API refuses a row carrying both
// flags with a 422, so the UI must not offer that combination as a reachable
// state -- and the three new strings must be TRANSLATED, not merely present.
//
// This file asserts the properties no existing gate can see:
//
//   * the mutual exclusion is applied at ONE producer, and no write surface
//     rolls its own phase-slot checkbox around it. A rule written out per
//     surface is a rule to get wrong four times rather than once, and three
//     of the four would still pass every behavioural test;
//   * the three keys resolve under BOTH namespaces in every catalogue.
//     `scripts/check-i18n-keys-resolve.mjs` cannot do this for the shared
//     control: it extracts the namespace by matching a string LITERAL, and
//     the control takes its namespace as a prop, so that gate skips the file
//     entirely;
//   * each key differs from its English source in every locale, with the one
//     exemption naming the string and the reason -- key parity is not
//     translation (#1915), and a catalogue holding the English string passes
//     the completeness gate while being untranslated;
//   * `phaseSlotExclusive` names the two CONTROLS, so it must quote the label
//     text that locale actually carries. A note telling the operator to look
//     for a control whose label does not exist in their language passes a
//     per-key check perfectly (#2198's `gitSshHint` lesson);
//   * `en-x-leet` is the documented substitution of the source rather than a
//     translation.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'

/** The two write surfaces, and the group the three keys sit under in each. */
const NAMESPACES: [string, string][] = [
  ['workspaceDetail', 'variables'],
  ['adminVariableSets', 'detail'],
]

const KEYS = ['configureOnly', 'configureOnlyHint', 'phaseSlotExclusive'] as const

/** The offered locales, read from the source of truth rather than listed here. */
const offered = (() => {
  const config = readFileSync('src/i18n/config.ts', 'utf8')
  const block = config.match(/export const locales = \[([\s\S]*?)\] as const/)
  assert.ok(block, 'could not find the `locales` array in src/i18n/config.ts')
  return [...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1])
})()

const load = (loc: string) => JSON.parse(readFileSync(`messages/${loc}.json`, 'utf8'))

const slot = (doc: Record<string, any>, group: string, leaf: string) =>
  (doc?.[group]?.[leaf] ?? {}) as Record<string, string>

const EN = Object.fromEntries(
  NAMESPACES.map(([g, l]) => [`${g}.${l}`, slot(load('en'), g, l)]),
)

/**
 * The locales a translation is expected of: everything offered except the
 * source itself, and except `en-GB` -- a dialect override carrying only the
 * spelling deltas from the American source. None of these three differs
 * between British and American English, so it is expected to carry none of
 * them and its absence is not a gap.
 */
const TRANSLATED = offered.filter((l) => l !== 'en' && l !== 'en-GB')

/**
 * Values that are legitimately the English string, each with its reason.
 *
 * `en-x-marklar` swaps NOUNS only and already keeps `Configure` as the verb in
 * `workspaceDetail.runs.queueConfigure`. Swapping it here would render
 * "Marklar only" -- byte-identical to that locale's `applyOnly` -- leaving two
 * indistinguishable labels on one form and a note that reads "Marklar only
 * reaches a marklar and a marklar".
 */
const IDENTICAL_BY_DESIGN: Record<string, Set<string>> = {
  'en-x-marklar': new Set(['configureOnly']),
}

test('the subject is exactly the three keys this change adds, in both namespaces', () => {
  // A derived gate passes on nothing when its set empties, and on a SHRUNKEN
  // set while staying above a floor. So assert the membership: a key added to
  // or dropped from the phase-slot controls fails here and is a deliberate act.
  for (const [group, leaf] of NAMESPACES) {
    const en = EN[`${group}.${leaf}`]
    for (const key of KEYS) {
      assert.equal(
        typeof en[key],
        'string',
        `en.${group}.${leaf}.${key} is missing -- the source catalogue is the subject`,
      )
    }
    // `applyOnly` is the slot these sit beside; without it the note below has
    // no label to quote and this file is testing half a feature.
    assert.equal(typeof en.applyOnly, 'string', `en.${group}.${leaf}.applyOnly is missing`)
  }
  assert.equal(KEYS.length, 3)
})

test('the three keys resolve under both namespaces in every catalogue', () => {
  // What `check-i18n-keys-resolve.mjs` would have covered, had the shared
  // control's namespace been a literal it could extract. The control is
  // mounted with "workspaceDetail.variables" on one page and
  // "adminVariableSets.detail" on the other, so a key present in one and
  // absent from the other renders MISSING_MESSAGE on exactly one page.
  const missing: string[] = []
  for (const loc of TRANSLATED) {
    const doc = load(loc)
    for (const [group, leaf] of NAMESPACES) {
      const node = slot(doc, group, leaf)
      for (const key of KEYS) {
        if (typeof node[key] !== 'string' || node[key].trim() === '') {
          missing.push(`${loc}: ${group}.${leaf}.${key}`)
        }
      }
    }
  }
  assert.deepEqual(missing, [], `keys missing from a catalogue:\n  ${missing.join('\n  ')}`)
})

test('both namespaces carry the same wording in every locale', () => {
  // The two surfaces write the same column and the shared edit panel is used
  // from both pages, so a variable that is "Configure only" on a workspace
  // must not be something else on a variable set.
  const drift: string[] = []
  for (const loc of ['en', ...TRANSLATED]) {
    const doc = load(loc)
    const [[g1, l1], [g2, l2]] = NAMESPACES
    const a = slot(doc, g1, l1)
    const b = slot(doc, g2, l2)
    for (const key of [...KEYS, 'applyOnly']) {
      if (a[key] !== b[key]) drift.push(`${loc}.${key}: ${a[key]} !== ${b[key]}`)
    }
  }
  assert.deepEqual(drift, [], `the two namespaces disagree:\n  ${drift.join('\n  ')}`)
})

test('each key is translated, not the English string copied', () => {
  const untranslated: string[] = []
  for (const loc of TRANSLATED) {
    const doc = load(loc)
    const exempt = IDENTICAL_BY_DESIGN[loc] ?? new Set<string>()
    for (const [group, leaf] of NAMESPACES) {
      const node = slot(doc, group, leaf)
      const en = EN[`${group}.${leaf}`]
      for (const key of KEYS) {
        if (node[key] === en[key] && !exempt.has(key)) {
          untranslated.push(`${loc}: ${group}.${leaf}.${key}`)
        }
      }
    }
  }
  assert.deepEqual(
    untranslated,
    [],
    `identical to English and not exempted:\n  ${untranslated.join('\n  ')}`,
  )
})

test('an exemption names a key this change adds, and is still identical', () => {
  // A ledger the gate trusts, so the half that is checkable is checked: an
  // exemption for a key that has since been translated is stale and must be
  // removed, and one naming a key outside this set is a typo nobody would see.
  for (const [loc, keys] of Object.entries(IDENTICAL_BY_DESIGN)) {
    assert.ok(TRANSLATED.includes(loc), `${loc} is exempted but is not an offered locale`)
    const doc = load(loc)
    for (const key of keys) {
      assert.ok(KEYS.includes(key as (typeof KEYS)[number]), `${loc}: ${key} is not one of these keys`)
      for (const [group, leaf] of NAMESPACES) {
        assert.equal(
          slot(doc, group, leaf)[key],
          EN[`${group}.${leaf}`][key],
          `${loc}: ${group}.${leaf}.${key} is exempted but is no longer identical -- drop the exemption`,
        )
      }
    }
  }
})

test('the exclusion note quotes the two controls the locale actually labels', () => {
  // `en-x-yoda` reorders clauses, so the label's trailing ", it is" cannot
  // survive inside a sentence; the label STEM is what an operator matches
  // against the screen, and that is what is required there.
  const STEM_ONLY = new Set(['en-x-yoda'])
  const broken: string[] = []
  for (const loc of ['en', ...TRANSLATED]) {
    const doc = load(loc)
    for (const [group, leaf] of NAMESPACES) {
      const node = slot(doc, group, leaf)
      const note = node.phaseSlotExclusive ?? ''
      const wanted = STEM_ONLY.has(loc)
        ? [node.applyOnly.split(',')[0], node.configureOnly.split(',')[0]]
        : [node.applyOnly, node.configureOnly]
      for (const label of wanted) {
        if (!note.includes(label)) broken.push(`${loc}: ${group}.${leaf} note omits ${label}`)
      }
    }
  }
  assert.deepEqual(broken, [], `the note names a control it does not label:\n  ${broken.join('\n  ')}`)
})

test('en-x-leet is the substitution of the source, not a translation', () => {
  // a e i o s t -> 4 3 1 0 5 7, CASE-INSENSITIVELY: a leading capital is
  // substituted too (`Agents` is `4g3n75`). None of these three carries an ICU
  // placeholder or a `<code>` span, which is where the whole-catalogue form of
  // this rule has its documented exceptions -- so equality is the right
  // assertion for exactly these keys.
  const MAP: Record<string, string> = { a: '4', e: '3', i: '1', o: '0', s: '5', t: '7' }
  const leet = (s: string) =>
    [...s].map((c) => MAP[c.toLowerCase()] ?? c).join('')
  const doc = load('en-x-leet')
  for (const [group, leaf] of NAMESPACES) {
    const node = slot(doc, group, leaf)
    const en = EN[`${group}.${leaf}`]
    for (const key of KEYS) {
      assert.equal(node[key], leet(en[key]), `en-x-leet ${group}.${leaf}.${key}`)
    }
  }
})

test('the mutual exclusion has ONE producer, and every surface goes through it', () => {
  // The rule itself: ticking either flag disables the other, so the operator
  // cannot build the state the API refuses with a 422. Asserted structurally
  // because a behavioural test of one surface says nothing about the other
  // three -- and the obvious wrong fix is to write the pair out per surface.
  const producer = readFileSync('src/components/phase-slot-flags.tsx', 'utf8')
  assert.match(
    producer,
    /checked=\{state\.applyOnly\}[\s\S]{0,120}?disabled=\{state\.configureOnly\}/,
    'the apply-only checkbox is not disabled while configure-only is set',
  )
  assert.match(
    producer,
    /checked=\{state\.configureOnly\}[\s\S]{0,120}?disabled=\{state\.applyOnly\}/,
    'the configure-only checkbox is not disabled while apply-only is set',
  )

  // Nothing else may render one of these checkboxes. Matched on the `checked`
  // binding rather than on an import, because a surface that rolled its own
  // would not import anything.
  const files = sourceFiles('src')
  const rogue = files.filter(
    (f) =>
      f !== 'src/components/phase-slot-flags.tsx' &&
      /checked=\{[^}]*(applyOnly|configureOnly|ApplyOnly|ConfigureOnly)/.test(
        readFileSync(f, 'utf8'),
      ),
  )
  assert.deepEqual(
    rogue,
    [],
    `a phase-slot checkbox outside the shared control:\n  ${rogue.join('\n  ')}`,
  )

  // And a floor, so a renamed component cannot make this pass on nothing: the
  // four write surfaces are the two add forms on the workspace page, the
  // variable-set add form, and the shared edit panel.
  const users = files.filter((f) => /<PhaseSlotFlags\b/.test(readFileSync(f, 'utf8')))
  assert.ok(
    users.length >= 3,
    `expected the shared control on at least 3 surfaces, found ${users.length}: ${users.join(', ')}`,
  )
})

/** Every .ts/.tsx under a directory, so the sweep above is derived, not listed. */
function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir)) {
    const p = `${dir}/${entry}`
    if (statSync(p).isDirectory()) out.push(...sourceFiles(p))
    else if (p.endsWith('.ts') || p.endsWith('.tsx')) out.push(p)
  }
  return out
}
