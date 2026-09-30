// The per-engine phase vocabulary, and the two ways it silently breaks (#1911).
//
// A run's phase words are keys, not literals — `phases.<engine>.words.<name>` —
// so nothing about them is type-checked. Two failures follow from that, and
// neither shows up as an error:
//
//  1. A key present for one engine and absent for the other. The catalogue
//     completeness gate compares locales against `en`, not engines against each
//     other, so `en` itself carrying a Terraform-only key passes every gate and
//     then renders the raw key on a Pulumi run.
//  2. A key some component asks for that no engine has. The i18n resolve gate
//     skips dynamically-built keys on purpose, so it cannot see these.
//
// Both are structural, so they are checked here rather than left to an E2E run
// that only visits the handful of pages a spec happens to open.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'

import {
  DEFAULT_ENGINE,
  PHASE_STATUSES,
  engineWord,
  phaseKey,
  vocabularyFor,
} from '../src/lib/phase-vocabulary.ts'

const en = JSON.parse(readFileSync('messages/en.json', 'utf8')) as {
  phases: Record<string, Record<string, Record<string, string>>>
}

test('both engines carry exactly the same vocabulary keys', () => {
  const terraform = Object.keys(en.phases.terraform.words).sort()
  const pulumi = Object.keys(en.phases.pulumi.words).sort()
  assert.ok(terraform.length > 0, 'the terraform words block is empty')
  // A key on one side only is the bug: the other engine renders the raw key.
  assert.deepEqual(
    pulumi,
    terraform,
    'phases.terraform.words and phases.pulumi.words must hold the same keys',
  )
})

test('every group both engines define is defined by both', () => {
  assert.deepEqual(
    Object.keys(en.phases.pulumi).sort(),
    Object.keys(en.phases.terraform).sort(),
  )
})

test('no vocabulary string is empty, and none is the key itself', () => {
  for (const engine of ['terraform', 'pulumi']) {
    for (const [key, value] of Object.entries(en.phases[engine].words)) {
      assert.equal(typeof value, 'string', `${engine}.${key} is not a string`)
      assert.notEqual(value.trim(), '', `${engine}.${key} is empty`)
      assert.ok(!value.includes('phases.'), `${engine}.${key} looks like a raw key`)
    }
  }
})

test('the two engines actually differ — a copied block would defeat the point', () => {
  const differing = Object.keys(en.phases.terraform.words).filter(
    (k) => en.phases.terraform.words[k] !== en.phases.pulumi.words[k],
  )
  // Not all of them: the two `*Placeholder` entries are code examples, and a
  // future entry might legitimately read the same either way. Most must differ,
  // or the vocabulary has been filled by copy and says nothing.
  assert.ok(
    differing.length > Object.keys(en.phases.terraform.words).length / 2,
    `only ${differing.length} of the words differ between engines`,
  )
})

test('an unknown or absent engine falls back to Terraform rather than a raw key', () => {
  assert.equal(vocabularyFor(undefined), DEFAULT_ENGINE)
  assert.equal(vocabularyFor(null), DEFAULT_ENGINE)
  assert.equal(vocabularyFor(''), DEFAULT_ENGINE)
  assert.equal(vocabularyFor('ansible'), DEFAULT_ENGINE)
  assert.equal(vocabularyFor('PULUMI'), 'pulumi')
  assert.equal(phaseKey('ansible', 'words', 'queuePlan'), 'phases.terraform.words.queuePlan')
  assert.equal(phaseKey('pulumi', 'words', 'queuePlan'), 'phases.pulumi.words.queuePlan')
})

test('engineWord resolves through the engine it is given', () => {
  const t = (key: string) => key
  assert.equal(engineWord(t, 'pulumi', 'queueRun'), 'phases.pulumi.words.queueRun')
  assert.equal(engineWord(t, undefined, 'queueRun'), 'phases.terraform.words.queueRun')
})

test('only the four phase statuses are redirected to the engine', () => {
  // A run is also pending, queued, confirmed, errored, canceled, discarded and
  // canceling — all the platform's own, and all reading the same either way.
  assert.deepEqual([...PHASE_STATUSES].sort(), ['applied', 'applying', 'planned', 'planning'])
  for (const s of ['pending', 'queued', 'confirmed', 'errored', 'canceled', 'discarded']) {
    assert.ok(!PHASE_STATUSES.has(s), `${s} is not a phase word`)
  }
})

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (p.endsWith('.tsx') || p.endsWith('.ts')) out.push(p)
  }
  return out
}

test('every vocabulary name a component asks for exists for both engines', () => {
  // The names are literals at the call site even though the key is built, so
  // they can be extracted — which is the whole reason `engineWord` takes a name
  // rather than a pre-built key.
  const names = new Set<string>()
  for (const file of walk('src')) {
    const src = readFileSync(file, 'utf8')
    for (const m of src.matchAll(/engineWord\(\s*\w+\s*,\s*[\w?.[\]']+\s*,\s*'([^']+)'/g)) {
      names.add(m[1])
    }
    for (const m of src.matchAll(/phaseKey\([^,]+,\s*'words'\s*,\s*'([^']+)'/g)) {
      names.add(m[1])
    }
    for (const m of src.matchAll(/\bword\('([^']+)'\)/g)) names.add(m[1])
    for (const m of src.matchAll(/\bphaseWord\('([^']+)'\)/g)) names.add(m[1])
    for (const m of src.matchAll(/\bphaseKeyFor\('([^']+)'\)/g)) names.add(m[1])
  }
  assert.ok(names.size > 20, `only found ${names.size} vocabulary call sites — extraction broke`)
  const missing = [...names].filter(
    (n) => !(n in en.phases.terraform.words) || !(n in en.phases.pulumi.words),
  )
  assert.deepEqual(missing, [], 'components ask for vocabulary names no engine defines')
})

// The status pill is rendered from a SET of phase tokens in one component and a
// GROUP of catalogue keys in another, and nothing made the two agree. They did
// not: the set held the two in-progress tokens and the group held the same two,
// so `planned` and `applied` fell through to the platform namespace and read
// "Planned" on a Pulumi workspace — on the workspace LIST, while the same run
// read "Previewed" on that workspace's own runs tab. The defect #1911 is about,
// one navigation step apart rather than one screen, found by looking at the
// running UI rather than by any test.
//
// Asserted as an equality in both directions: a token added to the namespace
// without widening the set renders the platform word, and a token added to the
// set without the namespace renders a raw key. Neither is visible from the other
// file, which is why this lives here and not beside either of them.
test('the badge status set and the engine status namespace name the same tokens', () => {
  const badge = readFileSync(
    join(import.meta.dirname, '../src/components/workspace-status-badges.tsx'),
    'utf8',
  )
  const m = badge.match(/const PHASE_FILTERS = new Set\(\[([^\]]*)\]\)/)
  assert.ok(m, 'PHASE_FILTERS is not a literal Set any more — this guard reads it as source')
  const filters = new Set([...m[1].matchAll(/'([^']+)'/g)].map((x) => x[1]))

  const en = JSON.parse(
    readFileSync(join(import.meta.dirname, '../messages/en.json'), 'utf8'),
  )
  for (const engine of ['terraform', 'pulumi']) {
    const group = new Set(Object.keys(en.phases[engine].status ?? {}))
    assert.deepEqual(
      [...group].sort(),
      [...filters].sort(),
      `phases.${engine}.status and PHASE_FILTERS disagree — a token in one and not the ` +
        `other either renders the platform word on every engine, or renders a raw key`,
    )
  }
})

// And the point of all of it: on the pill itself, the two engines must actually
// say different things. The keys can line up perfectly and still both read
// "Planned", which is what shipped.
test('every phase status the badge shows reads differently on the two engines', () => {
  const en = JSON.parse(
    readFileSync(join(import.meta.dirname, '../messages/en.json'), 'utf8'),
  )
  const tf = en.phases.terraform.status
  const pu = en.phases.pulumi.status
  for (const token of Object.keys(tf)) {
    assert.notEqual(
      pu[token],
      tf[token],
      `status "${token}" reads "${tf[token]}" on both engines, so the pill tells a Pulumi ` +
        `operator their run was ${tf[token]} — the word their runs tab does not use`,
    )
  }
})
