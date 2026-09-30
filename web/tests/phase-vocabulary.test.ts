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
