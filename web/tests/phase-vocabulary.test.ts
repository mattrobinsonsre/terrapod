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

// ---------------------------------------------------------------------------
// The same property, across every catalogue rather than just `en` (#1915).
//
// Everything above reads `en`, where the vocabulary was correct from the start.
// It was not correct anywhere else: 26 of the 33 locales rendered a Pulumi run
// in Terraform's words, and 11 more had done the plan half and not the apply
// half — so a German operator watched a run go `Gepreviewt` … `Angewendet`,
// half translated, with no gate anywhere that could notice.
//
// It could not be noticed because the completeness gate compares each locale
// against `en` KEY BY KEY. A locale holding `phases.pulumi.status.applied` with
// the Terraform word in it has the key, so it passes — the gate has no opinion
// about the value, and the value was the whole feature.
//
// English distinguishes all 76, which makes the property here unconditional
// rather than a judgement call: if English needed two words, so does every
// other language, and a locale where the two are equal has simply not been
// translated. That is also why this catches the trap the issue names — filling
// `words` and leaving `runStatus`/`status`/`activity` renders the phase one way
// on a run's own page and the other way on the workspace list, which is the
// original defect rebuilt by a partial fix of it.
// `en-x-marklar` replaces NOUNS with "marklar" and leaves verbs alone, so where
// the only English difference is a noun — "Apply complete" / "Update complete",
// "plan + apply" / "preview + update" — both engines collapse to the same
// string and the identity is the dialect working, not a gap. Where the
// difference is a verb or participle it survives and must still be made:
// `Applied`/`Updated` and `can update`/`can apply` do differ.
//
// Pinned as an explicit set rather than exempting the locale, because the
// interesting failure is a member being ADDED — a verb collapsing here would be
// a real regression, and a wildcard would swallow it silently.
const MARKLAR_NOUN_COLLAPSE = new Set([
  'en-x-marklar:activity.applying',
  'en-x-marklar:activity.planning',
  'en-x-marklar:words.activityApplied',
  'en-x-marklar:words.activityConfirmed',
  'en-x-marklar:words.activityDiscarded',
  'en-x-marklar:words.activityPlannedComplete',
  'en-x-marklar:words.activityPlannedSpeculative',
  'en-x-marklar:words.aiSummaryTitle',
  'en-x-marklar:words.badgePlanOnly',
  'en-x-marklar:words.changesPlanning',
  'en-x-marklar:words.confirmDiscard',
  'en-x-marklar:words.costNoState',
  'en-x-marklar:words.detailsPlanOnly',
  'en-x-marklar:words.discardedHeading',
  'en-x-marklar:words.driftRemediate',
  'en-x-marklar:words.driftRemediateConfirm',
  'en-x-marklar:words.driftRemediateTitle',
  'en-x-marklar:words.errorsQueuePlan',
  'en-x-marklar:words.errorsQueuePlanStatus',
  'en-x-marklar:words.fieldsAutoMerge',
  'en-x-marklar:words.lockLockedDesc',
  'en-x-marklar:words.logApplyEmpty',
  'en-x-marklar:words.logIndexSummary',
  'en-x-marklar:words.logIndexTitle',
  'en-x-marklar:words.logPlanEmpty',
  'en-x-marklar:words.planExpiryTitle',
  'en-x-marklar:words.planOptions',
  'en-x-marklar:words.planSummaryChatPlaceholder',
  'en-x-marklar:words.planSummaryHeading',
  'en-x-marklar:words.planSummaryNoChangesTitle',
  'en-x-marklar:words.planSummaryRegenerateTooltip',
  'en-x-marklar:words.planSummarySummarising',
  'en-x-marklar:words.queueApplyConfirm',
  'en-x-marklar:words.queueApplyRefBlocked',
  'en-x-marklar:words.queueApplyTitle',
  'en-x-marklar:words.queueDestroyTitle',
  'en-x-marklar:words.queuePlan',
  'en-x-marklar:words.queuePlanConfirm',
  'en-x-marklar:words.queueRun',
  'en-x-marklar:words.runTriggersDescription',
  'en-x-marklar:words.slackDescription',
  'en-x-marklar:words.tabApplyFull',
  'en-x-marklar:words.tabApplyLabel',
  'en-x-marklar:words.tabPlanFull',
  'en-x-marklar:words.tabPlanLabel',
  'en-x-marklar:words.timelinePlanComplete',
])

const IDENTICAL_BY_DESIGN: Record<string, string> = {
  // 'xx:group.key': 'why the two engines legitimately read the same here',
}

test('every locale tells the two engines apart on every phase key', () => {
  const dir = join(import.meta.dirname, '../messages')
  const offenders: string[] = []
  const unusedExemptions = new Set(Object.keys(IDENTICAL_BY_DESIGN))
  let checked = 0

  for (const file of readdirSync(dir).filter((f) => f.endsWith('.json'))) {
    const locale = file.replace(/\.json$/, '')
    const phases = JSON.parse(readFileSync(join(dir, file), 'utf8')).phases
    // en-GB carries only the handful of strings whose British spelling differs,
    // and deep-merges the rest over `en`. A group it does not define is not a
    // gap, so only what a catalogue actually declares is examined.
    if (!phases?.terraform || !phases?.pulumi) continue

    for (const group of Object.keys(phases.terraform)) {
      for (const [key, tf] of Object.entries(phases.terraform[group])) {
        const pu = phases.pulumi[group]?.[key]
        if (pu === undefined) continue
        checked++
        const id = `${locale}:${group}.${key}`
        if (tf !== pu) {
          // A listed exemption that no longer applies is removed, not left: it
          // would go on excusing whatever later collapses onto that key.
          assert.ok(
            !(id in IDENTICAL_BY_DESIGN),
            `${id} is exempted as identical by design but the two engines now differ — ` +
              `drop the entry from IDENTICAL_BY_DESIGN`,
          )
          assert.ok(
            !MARKLAR_NOUN_COLLAPSE.has(id),
            `${id} now differs — drop it from MARKLAR_NOUN_COLLAPSE`,
          )
          continue
        }
        if (MARKLAR_NOUN_COLLAPSE.has(id)) continue
        if (id in IDENTICAL_BY_DESIGN) {
          unusedExemptions.delete(id)
          continue
        }
        offenders.push(`${id} — both read ${JSON.stringify(tf)}`)
      }
    }
  }

  assert.ok(checked > 2000, `only ${checked} values examined — the catalogue walk broke`)
  // Listed rather than diffed, and capped: a regression here is usually one
  // locale, but the first run of this guard found 1,400 — and a deepEqual
  // against [] printed every one of them, which buries the count that matters.
  const shown = offenders.slice(0, 40)
  const rest = offenders.length - shown.length
  assert.ok(
    offenders.length === 0,
    `${offenders.length} phase values read the same on both engines, so a Pulumi run is ` +
      `described to the operator in Terraform's words. Translate the pulumi side, or — if ` +
      `the two genuinely read the same in that language — add the key to ` +
      `IDENTICAL_BY_DESIGN with the reason.\n  ${shown.join('\n  ')}` +
      (rest > 0 ? `\n  …and ${rest} more` : ''),
  )
  assert.deepEqual(
    [...unusedExemptions],
    [],
    'these exemptions no longer match anything; a stale one hides a real regression',
  )
})

// The half-translated case, which the guard above cannot see (#1915).
//
// That one asks whether the two engines differ. `Plan + apply` becoming
// `Preview + apply` satisfies it completely — one word of two was swapped, the
// values are now different, and the label still promises a Pulumi operator an
// apply their engine does not have. Eleven locales shipped exactly that, and
// five more acquired it while being translated *by this issue's own work*,
// which is how much of a trap it is.
//
// English is the arbiter rather than a word list: where the English Pulumi
// value dropped a Terraform term, a locale still holding it is a leftover; where
// English kept one, keeping it is correct. Only two English Pulumi values keep
// one, and both are the *pertains* sense ("Applies to apply-capable runs only",
// "RBAC does not apply to…") sitting next to the phase sense — the two places a
// translator most easily swaps the wrong instance.
//
// KNOWN GAP, stated rather than papered over: it only catches a term the locale
// left in ENGLISH. A locale that translated apply and then left its own word in
// a Pulumi sentence passes — Polish `lockLockedDesc` read "previewów ani
// zastosowań" and this check is blind to it. That is the common case for the
// bug in practice, because the compound labels (`Plan + apply`) are exactly the
// strings locales keep untranslated, but it is not the whole of it.
//
// Closing the gap was tried and rejected: deriving each locale's own apply stem
// from its Terraform side covers only two thirds of them (ten have no common
// stem across the inflected forms at all), and every hit it produced here was
// legitimate — the two pertains-sense keys, plus "the Slack app" matching in
// French, Turkish and Chinese, where the word for *application* is the word for
// *apply*. A gate whose allowlist is entirely false positives teaches people to
// ignore it, which costs more than the coverage buys.
const TERRAFORM_TERMS =
  /\b(?:appl(?:y|ies|ied|ying)|plans?|planned|planning)\b/gi

function terraformTermsIn(value: string): Set<string> {
  return new Set([...value.matchAll(TERRAFORM_TERMS)].map((m) => m[0].toLowerCase()))
}

const LEFTOVER_BY_DESIGN: Record<string, string> = {
  // 'xx:group.key': 'why this locale keeps a term English dropped',
}

test('no locale leaves a Terraform term in a Pulumi value that English dropped', () => {
  const dir = join(import.meta.dirname, '../messages')
  const en = JSON.parse(readFileSync(join(dir, 'en.json'), 'utf8')).phases
  const offenders: string[] = []
  const unused = new Set(Object.keys(LEFTOVER_BY_DESIGN))

  for (const file of readdirSync(dir).filter((f) => f.endsWith('.json') && f !== 'en.json')) {
    const locale = file.replace(/\.json$/, '')
    const phases = JSON.parse(readFileSync(join(dir, file), 'utf8')).phases
    if (!phases?.pulumi) continue
    for (const group of Object.keys(phases.pulumi)) {
      for (const [key, value] of Object.entries(phases.pulumi[group]) as [string, string][]) {
        const allowed = terraformTermsIn(en.pulumi[group]?.[key] ?? '')
        const left = [...terraformTermsIn(value)].filter((t) => !allowed.has(t))
        const id = `${locale}:${group}.${key}`
        if (left.length === 0) {
          assert.ok(
            !(id in LEFTOVER_BY_DESIGN),
            `${id} is exempted but no longer keeps a Terraform term — drop the entry`,
          )
          continue
        }
        if (id in LEFTOVER_BY_DESIGN) {
          unused.delete(id)
          continue
        }
        offenders.push(`${id} keeps ${left.join('/')} — ${JSON.stringify(value.slice(0, 90))}`)
      }
    }
  }

  const shown = offenders.slice(0, 40)
  const rest = offenders.length - shown.length
  assert.ok(
    offenders.length === 0,
    `${offenders.length} Pulumi values keep a Terraform term the English Pulumi value dropped, ` +
      `so the phrase is only half translated — a Pulumi run has no apply and no plan. ` +
      `Compare each against its English Pulumi value and move the whole phrase, or add the ` +
      `key to LEFTOVER_BY_DESIGN with the reason.\n  ${shown.join('\n  ')}` +
      (rest > 0 ? `\n  …and ${rest} more` : ''),
  )
  assert.deepEqual([...unused], [], 'stale exemptions hide real regressions')
})

// The tab label's <log> chunk is joined with a space the COMPONENT supplies.
//
//   log: (chunks) => <span className="hidden md:inline"> {chunks}</span>
//
// That literal space is what makes `Apply<log>Log</log>` read "Apply Log" on a
// wide screen and "Apply" on a phone — one string, two renderings. It also
// means the catalogue does not get to choose the separator, so a translator who
// writes the separator themselves gets two: German's `Plan<log>-Protokoll</log>`
// rendered "Plan -Protokoll", and had done in every release since the label was
// introduced, on both engines, because it is invisible in the JSON and nothing
// compared the tag content against the component that consumes it.
//
// Checked here rather than left to a screenshot: it is a property of the two
// files together, and neither file is wrong on its own.
test('a tab label never supplies its own separator — the component already does', () => {
  const dir = join(import.meta.dirname, '../messages')
  const offenders: string[] = []
  for (const file of readdirSync(dir).filter((f) => f.endsWith('.json'))) {
    const phases = JSON.parse(readFileSync(join(dir, file), 'utf8')).phases
    if (!phases) continue
    for (const engine of Object.keys(phases)) {
      for (const key of ['tabPlanLabel', 'tabApplyLabel']) {
        const value: string | undefined = phases[engine]?.words?.[key]
        if (value === undefined) continue
        const m = value.match(/^(.*?)<log>(.*?)<\/log>(.*)$/)
        if (!m) {
          offenders.push(`${file}:${engine}.${key} has no <log> chunk — ${JSON.stringify(value)}`)
          continue
        }
        const [, head, inner, tail] = m
        // The component renders head + " " + inner + tail.
        if (/^[^\p{L}\p{N}]/u.test(inner) || /\s$/.test(head) || /\s\s/.test(`${head} ${inner}${tail}`)) {
          offenders.push(
            `${file}:${engine}.${key} renders ${JSON.stringify(`${head} ${inner}${tail}`)} ` +
              `from ${JSON.stringify(value)}`,
          )
        }
      }
    }
  }
  assert.deepEqual(
    offenders,
    [],
    'a <log> chunk opening with punctuation (or a head ending in a space) renders with a ' +
      'doubled separator, because the component supplies one of its own',
  )
})

// The English-derived locales do not carry a stem that only made sense as
// "plan" or "apply" (#1915 follow-up).
//
// Five locales shipped "Previewning", "Previewned", "Uping", "Upin''", "Upd"
// and Dutch "Previewnen" — the output of substituting Plan→Preview and
// Apply→Up into the *inflected* Terraform form. "Planning" is "Plan" + "ning",
// so the swap lands mid-word and leaves a non-word. These render in the run
// header, the most-read string on the page, and each locale already had the
// right form elsewhere in its own file.
//
// Deliberately a deny-list of the broken shapes rather than a rule derived from
// the Terraform string: deriving it flags every locale whose correct form IS the
// naive substitution, which is common and right for loan morphology — Polish
// "previewu" is the genuine genitive, Norwegian "Previewsammendrag" a genuine
// compound. Telling those apart needs the language, not a regex. The wider
// question of whether other locales carry the same defect is tracked separately;
// this guard only holds the ground that was cleared.
test('no locale carries a stem that only parsed as "plan" or "apply"', () => {
  const dir = join(import.meta.dirname, '../messages')
  // "Previewn" catches Previewning/Previewned/Previewnin/Previewnen; the rest
  // are whole values, since "Upd" is a prefix of the correct "Updating".
  const MANGLED_SUBSTRING = /Previewn/i
  const MANGLED_WHOLE = new Set(['Uping', 'Upin', "Upin''", 'Upd'])

  const offenders: string[] = []
  for (const file of readdirSync(dir).filter((f) => f.endsWith('.json'))) {
    const phases = JSON.parse(readFileSync(join(dir, file), 'utf8')).phases
    if (!phases?.pulumi) continue
    for (const group of ['runStatus', 'status', 'activity', 'words']) {
      const values = phases.pulumi[group]
      if (!values) continue
      for (const [key, value] of Object.entries(values)) {
        if (typeof value !== 'string') continue
        if (MANGLED_SUBSTRING.test(value) || MANGLED_WHOLE.has(value)) {
          offenders.push(`${file}:${group}.${key} = ${JSON.stringify(value)}`)
        }
      }
    }
  }
  assert.deepEqual(
    offenders,
    [],
    'these are the Terraform string with the verb substituted inside a word, which leaves ' +
      "a non-word. Use the locale's own inflected form — it is attested elsewhere in the " +
      'same file (for example its timelinePlanningStarted).',
  )
})
