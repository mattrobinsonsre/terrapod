// Two indicators, never one severity scale (#2589).
//
// A single badge meaning both "signature verification FAILED" and "has 4 known
// CVEs" teaches a reviewer the opposite of what we intend: it makes an
// advisory-by-design finding look like an incident, and buries the real
// verification failure among many of them.
//
// This is a STRUCTURAL gate on the source rather than a rendering test, for the
// same reason `status-dot-pairing.test.ts` is: the property is "these two
// things are styled apart and only one is critical", which a snapshot of
// rendered output states far less clearly than the source does.

import { strict as assert } from 'node:assert'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const SOURCE = readFileSync(join(ROOT, 'src/components/supply-chain-indicators.tsx'), 'utf8')
const EN = JSON.parse(readFileSync(join(ROOT, 'messages/en.json'), 'utf8')) as Record<
  string,
  Record<string, Record<string, string>>
>

test('verification has three states and exactly one of them is critical', () => {
  // `failed` is the only state that may carry critical styling. If `verified`
  // or `unverified` ever gains red, the badge stops meaning anything, because
  // the normal case would be shouting.
  const styles = SOURCE.slice(
    SOURCE.indexOf('VERIFICATION_STYLES'),
    SOURCE.indexOf('interface VerificationBadgeProps'),
  )
  for (const state of ['verified', 'unverified', 'failed']) {
    assert.ok(styles.includes(`${state}:`), `${state} has no style`)
  }
  const redStates = ['verified', 'unverified', 'failed'].filter((s) => {
    const line = styles.split('\n').find((l) => l.trim().startsWith(`${s}:`)) ?? ''
    return line.includes('red-')
  })
  assert.deepEqual(redStates, ['failed'], 'exactly `failed` may be styled critical')
})

test('every state renders its WORD, never colour alone', () => {
  // No single mid-chroma fill clears 3:1 against both themes' surfaces — see
  // status-dot-pairing.test.ts — so the text is what states the status and the
  // colour reinforces it. The badge must render its label unconditionally.
  const badge = SOURCE.slice(
    SOURCE.indexOf('export function VerificationBadge'),
    SOURCE.indexOf('export interface SupplyChain'),
  )
  // In its OWN element, not a bare text node beside the glyph. The word is
  // what states the status, so it has to be addressable on its own — as a
  // sibling text node the badge's text content is "<glyph>Verified", and an
  // exact match for the state never finds it. Caught by an e2e assertion
  // before this gate existed.
  assert.ok(
    /<span>\{label\}<\/span>/.test(badge),
    'the label must be its own element, not a bare text node beside the glyph',
  )
  // The glyph is decorative and must be hidden from assistive tech, or a
  // screen reader announces a tick as well as the word.
  assert.ok(badge.includes('aria-hidden'), 'the glyph must be aria-hidden')
})

test('the not-inventoried state is reachable and is not a blank', () => {
  // A blank cell reads as safe. "We did not look" and "we looked and found
  // nothing" are the two facts a reviewer most needs told apart.
  const cell = SOURCE.slice(SOURCE.indexOf('export function SupplyChainCell'))
  assert.ok(cell.includes("'not-inventoried'"), 'the third state must be handled')
  assert.ok(cell.includes('labels.notInventoried'), 'it must render a word, not an empty cell')
  // A null supply must take the same branch rather than falling through to the
  // clean rendering, which would report an un-inventoried artifact as clean.
  assert.ok(cell.includes('!supply'), 'a missing supply-chain must render as not-inventoried')
})

test('malicious is surfaced apart from the severity count', () => {
  // A malicious package is not a defect in a dependency, it is the artifact
  // being hostile — and the one finding class that is NOT advisory by design.
  // Folding it into the count hides exactly that.
  const cell = SOURCE.slice(SOURCE.indexOf('export function SupplyChainCell'))
  assert.ok(cell.includes('supply.malicious > 0'), 'malicious needs its own branch')
  assert.ok(
    cell.indexOf('labels.malicious') > cell.indexOf('labels.findings'),
    'malicious is rendered beside the count, not instead of it',
  )
})

test('the counted labels are functions, so ICU plurals reach the formatter', () => {
  // A string with a {count} placeholder interpolated here would already have
  // been resolved against some other number by next-intl, and every row would
  // show that one. This caught a real bug during development.
  assert.ok(
    /findings:\s*\(count: number\)\s*=>\s*string/.test(SOURCE),
    'findings must be a function of the count',
  )
  assert.ok(
    /malicious:\s*\(count: number\)\s*=>\s*string/.test(SOURCE),
    'malicious must be a function of the count',
  )
  assert.ok(!SOURCE.includes(".replace('{count}'"), 'no placeholder may be string-replaced')
})

test('the English catalogue states a reason for every not-inventoried case', () => {
  // The state is only honest if the reason exists to go with it.
  // NESTED, not a flat dotted key: next-intl resolves by splitting on '.' and
  // traversing, so a flat `"reason.notAdmitted"` key is unreachable — which is
  // how this group shipped unrenderable in every locale until the resolve gate
  // caught it.
  const reason = EN.supplyChain?.reason
  assert.ok(reason, 'supplyChain.reason must be a nested group')
  for (const key of ['notAdmitted', 'awaiting']) {
    assert.ok(reason[key] && reason[key].length > 10, `reason.${key} must be a real sentence`)
  }
  // The two reasons must differ: one resolves itself and one never will.
  assert.notEqual(reason.notAdmitted, reason.awaiting)
})

test('the verdict line names its tools AND its database epoch', () => {
  // A clean result with no date is worthless; with the epoch and the versions
  // that produced it, it is evidence.
  const line = EN.supplyChain.detail.verdictLine
  for (const placeholder of ['{matcher}', '{matcherVersion}', '{epoch}', '{generator}', '{generatorVersion}']) {
    assert.ok(line.includes(placeholder), `the verdict line must carry ${placeholder}`)
  }
})
