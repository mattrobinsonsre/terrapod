import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

/**
 * The bind-plan note beside the Confirm button on the run page (#1911).
 *
 * A Pulumi workspace can lock its update to the preview that was approved, or
 * leave it to work out its own changes at apply time like a plain `pulumi up`.
 * Which one you are about to get is only visible at the moment you approve it,
 * so the note renders next to the action rather than on the settings page.
 *
 * Two properties are guarded here because neither is visible to any other gate:
 * the engine test, which is easy to "tidy" into silence, and the translations,
 * which key parity cannot judge.
 */

const WEB = fileURLToPath(new URL('..', import.meta.url))
const MESSAGES = join(WEB, 'messages')
const RUN_PAGE = join(WEB, 'src/app/workspaces/[id]/runs/[runId]/page.tsx')

test('the note is gated on the attribute being present, not on it being true', () => {
  // `pulumi-bind-plan` is null on every engine that has no such concept, so
  // `!= null` is what asks "is this a Pulumi run". Narrowing it to truthiness
  // reads like a harmless simplification and removes the note from exactly the
  // runs that most need it: the unbound ones, whose note is the warning.
  const src = readFileSync(RUN_PAGE, 'utf8')
  const gate = src
    .split('\n')
    .find((l) => l.includes("pulumi-bind-plan'] ") && l.includes('&&'))

  assert.ok(gate, 'the run page no longer gates a render on pulumi-bind-plan')
  assert.match(
    gate,
    /pulumi-bind-plan'\]\s*!=\s*null/,
    `the bind-plan note must test \`!= null\`, not truthiness — an unbound run ` +
      `has the attribute set to false, and gating on truthiness hides its ` +
      `warning. Found: ${gate.trim()}`,
  )
})

/** The source strings, and the locale that owns them. */
const SOURCE = JSON.parse(readFileSync(join(MESSAGES, 'en.json'), 'utf8')).runDetail.bindPlan
const SOURCE_LOCALE = 'en.json'

/**
 * en-GB carries only the spellings that differ from the American source, so a
 * string with no British/American difference is absent by design rather than
 * missing. It is gated as a subset everywhere else and is one here too.
 */
const SUBSET_LOCALES = new Set(['en-GB.json'])

function localeFiles(): string[] {
  return readdirSync(MESSAGES)
    .filter((f) => f.endsWith('.json') && f !== SOURCE_LOCALE && !SUBSET_LOCALES.has(f))
    .sort()
}

test('no locale left the bind-plan note in English', () => {
  // Key parity is not translation: a value copied across from the source passes
  // every completeness gate while reading as English to the person it was
  // translated for. Both strings are full sentences, so no locale — including
  // the novelty ones, which rewrite nouns or syntax — has a legitimate reason
  // to match the source.
  const untranslated: string[] = []
  for (const file of localeFiles()) {
    const bindPlan = JSON.parse(readFileSync(join(MESSAGES, file), 'utf8'))?.runDetail?.bindPlan
    if (!bindPlan) continue // absence is check-i18n-completeness's to report
    for (const key of ['bound', 'unbound'] as const) {
      if (bindPlan[key] === SOURCE[key]) untranslated.push(`${file} → ${key}`)
    }
  }
  assert.deepEqual(
    untranslated,
    [],
    `these values are still the English source verbatim: ${untranslated.join(', ')}`,
  )
})

test('every locale keeps `pulumi up` as the command it is', () => {
  // The one literal in these strings that is not prose. Translating, inflecting
  // or transliterating it — or, in the leet locale, encoding it — turns working
  // copy-pasteable advice into a command that does not exist.
  const mangled: string[] = []
  for (const file of [SOURCE_LOCALE, ...localeFiles()]) {
    const bindPlan = JSON.parse(readFileSync(join(MESSAGES, file), 'utf8'))?.runDetail?.bindPlan
    if (!bindPlan?.unbound) continue
    if (!bindPlan.unbound.includes('pulumi up')) mangled.push(file)
  }
  assert.deepEqual(mangled, [], `\`pulumi up\` did not survive translation in: ${mangled.join(', ')}`)
})
