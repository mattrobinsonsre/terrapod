import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'

import {
  ACTOR_TYPE_BADGES,
  ORDINARY_ACTOR_TYPE,
  actorFilterParam,
  actorTypeBadge,
} from '../src/lib/audit-actor.ts'

/**
 * The two things the audit log says about an actor (#2701).
 *
 * `actor-type` was served and surfaced nowhere, so an action driven from a
 * pull-request comment and one taken in the UI read identically; and the actor
 * box drove only `filter[actor]`, leaving `filter[actor-login]` reachable only
 * by hand-editing a URL.
 *
 * What is pinned here is deliberately the half the shipped gates cannot see:
 *
 *  - `i18n:check` compares key SETS, so it is silent about a novelty locale
 *    holding the English string. The per-locale assertions below are the only
 *    thing that says those six were transformed.
 *  - `check-i18n-keys-resolve.mjs` SKIPS a key built at runtime, and the badge
 *    label is exactly that -- a template literal keyed on the badge's own
 *    message key -- so nothing else
 *    proves those three keys exist in the catalogues at all.
 *  - no render assertion can see that only ONE of the two actor filters is
 *    ever sent, which is the invariant below.
 */

const MESSAGES = join(import.meta.dirname, '..', 'messages')
const PAGE = join(import.meta.dirname, '..', 'src', 'app', 'admin', 'audit-log', 'page.tsx')
const SRC = join(import.meta.dirname, '..', 'src')

const en = JSON.parse(readFileSync(join(MESSAGES, 'en.json'), 'utf8')).adminAuditLog

/** `adminAuditLog.actorType` + the five new `filters.*`, as the fill wrote them. */
const NEW_KEYS = [
  'filters.actor',
  'filters.actorKind',
  'filters.actorKindEmail',
  'filters.actorKindLogin',
  'filters.actorLoginPlaceholder',
  'actorType.vcsUser',
  'actorType.system',
  'actorType.slackUser',
] as const

function reach(doc: unknown, path: string): unknown {
  let at: unknown = doc
  for (const part of path.split('.')) {
    if (at === null || typeof at !== 'object') return undefined
    at = (at as Record<string, unknown>)[part]
  }
  return at
}

// ---------------------------------------------------------------- the badge

test('the ordinary actor type gets no badge, so the unusual ones stand out', () => {
  assert.equal(actorTypeBadge(ORDINARY_ACTOR_TYPE), null)
  assert.equal(actorTypeBadge(''), null)
  // The whole point: badging `terrapod_user` would badge almost every row.
  assert.ok(!(ORDINARY_ACTOR_TYPE in ACTOR_TYPE_BADGES))
})

test('every actor type the server writes has a badge', () => {
  // `actor_type` is written in exactly these values -- `audit_service`
  // (terrapod_user, vcs_user), the three `actor_type="system"` call sites, and
  // `slack_interactions._UNLINKED_ACTOR_TYPE`. A value added server-side with
  // no entry here must still be visible, which the next test covers.
  for (const type of ['vcs_user', 'system', 'slack_user']) {
    const badge = actorTypeBadge(type)
    assert.ok(badge, `${type} has no badge`)
    assert.ok(badge!.messageKey, `${type} has no message key`)
    assert.equal(badge!.raw, undefined)
  }
})

test('an unrecognised actor type shows its raw value rather than nothing', () => {
  // Falling back to NO badge would make a new server-side type look exactly
  // like the ordinary case -- the defect this badge exists to end, arriving
  // again through a value nobody added here.
  const badge = actorTypeBadge('machine_user')
  assert.ok(badge)
  assert.equal(badge!.raw, 'machine_user')
  assert.equal(badge!.messageKey, undefined)
})

test('every badge label resolves in every catalogue', () => {
  // The label is a RUNTIME key, which `check-i18n-keys-resolve.mjs` skips by
  // design -- so without this a renamed message key would render the literal
  // `actorType.vcsUser` in all 33 locales with every gate green.
  const files = readdirSync(MESSAGES).filter((f) => f.endsWith('.json'))
  assert.ok(files.length >= 33, `only ${files.length} catalogues found`)
  let checked = 0
  for (const file of files) {
    const doc = JSON.parse(readFileSync(join(MESSAGES, file), 'utf8'))
    const group = reach(doc, 'adminAuditLog.actorType')
    // en-GB is a dialect OVERRIDE carrying only the spelling deltas, and none
    // of these three has one, so a group it does not declare is not a gap.
    if (group === undefined) {
      assert.equal(file, 'en-GB.json', `${file} declares no adminAuditLog.actorType`)
      continue
    }
    for (const { messageKey } of Object.values(ACTOR_TYPE_BADGES)) {
      const value = reach(doc, `adminAuditLog.actorType.${messageKey}`)
      assert.equal(typeof value, 'string', `${file} has no actorType.${messageKey}`)
      assert.ok((value as string).length > 0, `${file} actorType.${messageKey} is empty`)
      checked++
    }
  }
  assert.ok(checked >= 96, `only ${checked} label(s) checked`)
})

test('the badge follows the house badge shape and invents no colour pairing', () => {
  // The decision was to be consistent with the badges already on this page and
  // elsewhere, which also means `check_theme_contrast.py` is already measuring
  // these pairings on the light theme -- a new pairing would be unmeasured.
  const page = readFileSync(PAGE, 'utf8')
  assert.match(
    page,
    /inline-flex items-center px-2 py-0\.5 rounded-full text-xs font-medium \$\{badge\.className\}/,
    'the actor-type badge does not use the house badge shape',
  )
  // Gated on the badge and on NOTHING ELSE. Asserting only the class string
  // above is what let `{false && badge && (` survive this gate's own mutation
  // check -- the badge was styled correctly and rendered never.
  assert.match(
    page,
    /\{badge (?:&& \(|\? \()/,
    'the badge render is gated on something other than `badge` -- the whole ' +
      'column goes back to being indistinguishable if that condition is false',
  )
  assert.match(
    page,
    /const badge = actorTypeBadge\(a\['actor-type'\]\)/,
    'the badge is not derived from the served `actor-type`',
  )

  const everywhere = readdirSync(SRC, { recursive: true, encoding: 'utf8' })
    .filter((f) => f.endsWith('.tsx') || f.endsWith('.ts'))
    .filter((f) => !f.endsWith('audit-actor.ts'))
    .map((f) => readFileSync(join(SRC, f), 'utf8'))
    .join('\n')

  const pairings = new Set(Object.values(ACTOR_TYPE_BADGES).map((b) => b.className))
  pairings.add('bg-slate-700 text-slate-400') // the unrecognised-type fallback
  assert.equal(pairings.size, 3, 'expected three distinct pairings')
  for (const pairing of pairings) {
    assert.ok(
      everywhere.includes(pairing),
      `${pairing} appears nowhere else in web/src -- reuse a shipped pairing ` +
        `rather than inventing one, so the contrast gate already measures it`,
    )
  }
})

// --------------------------------------------------------------- the filter

test('an actor value is sent as exactly ONE of the two served filters', () => {
  assert.equal(actorFilterParam('email'), 'filter[actor]')
  assert.equal(actorFilterParam('login'), 'filter[actor-login]')
  assert.notEqual(actorFilterParam('email'), actorFilterParam('login'))
})

test('the page reaches both filters, and only through the one producer', () => {
  const page = readFileSync(PAGE, 'utf8')
  // Sending both with one value asks for a row whose actor_email AND
  // actor_login are that value, and no row has both: the server ANDs the two
  // exact-match filters, and a vcs_user entry carries an empty email by
  // design. The pair matches NOTHING, which reads as "that actor did nothing".
  assert.ok(
    !/params\.set\(\s*'filter\[actor(-login)?\]'/.test(page),
    'the actor filter key is spelled literally -- it must come from ' +
      'actorFilterParam(), which can only ever return one of the two',
  )
  assert.match(page, /params\.set\(actorFilterParam\(actorKind\), actor\)/)
  // Both kinds are offered, or `filter[actor-login]` stays unreachable.
  assert.match(page, /<option value="email">/)
  assert.match(page, /<option value="login">/)
})

test('the actor filter label no longer says it means an email', () => {
  const page = readFileSync(PAGE, 'utf8')
  assert.ok(
    !page.includes('filters.actorEmail'),
    'the page still reads the old email-only label',
  )
  assert.equal(reach(en, 'filters.actor'), 'Actor')
  assert.ok(
    !/email/i.test(reach(en, 'filters.actor') as string),
    'the label still names the email, which is now only one of the two fields',
  )
})

// ------------------------------------------------- the novelty transforms

/** `a e i o s t -> 4 3 1 0 5 7`, CASE-INSENSITIVE: a leading capital IS substituted. */
function leet(value: string): string {
  const map: Record<string, string> = { a: '4', e: '3', i: '1', o: '0', s: '5', t: '7' }
  return [...value].map((c) => map[c.toLowerCase()] ?? c).join('')
}

test('en-x-leet substitutes every one of the new values', () => {
  // Scoped to these eight keys rather than the whole catalogue: ~74 of the
  // 3,000-odd leet values legitimately do not reproduce under a single rule
  // (a `<code>` span, an ICU sub-message), so a tree-wide equality assertion
  // would fail on correct translations.
  const doc = JSON.parse(readFileSync(join(MESSAGES, 'en-x-leet.json'), 'utf8'))
  for (const key of NEW_KEYS) {
    const source = reach(en, key) as string
    const got = reach(doc, `adminAuditLog.${key}`) as string
    assert.equal(got, leet(source), `en-x-leet ${key}`)
    assert.notEqual(got, source, `en-x-leet ${key} is still the English string`)
  }
})

/**
 * A novelty value that legitimately equals the English, with the reason.
 *
 * `VCS` is an initialism every one of the 31 catalogues keeps verbatim (they
 * all do in `nav.vcsConnections`) and `Slack` is a product name, so those two
 * are identical nearly everywhere and that is correct rather than a gap. What
 * this pins is that nothing ELSE slipped through untranslated.
 */
const NOVELTY_IDENTICAL: Record<string, string> = {
  'en-x-marklar:filters.actorLoginPlaceholder': 'an example login, like the kept user@example.com',
  'en-x-lolcat:filters.actor': 'this catalogue already keeps Actor verbatim',
  'en-x-lolcat:filters.actorLoginPlaceholder': 'an example login',
  'en-x-lolcat:actorType.vcsUser': 'an initialism',
  'en-x-lolcat:actorType.slackUser': 'a product name',
  'en-x-pirate:filters.actor': 'this catalogue already keeps Actor verbatim',
  'en-x-pirate:filters.actorKind': 'no pirate form; Kind is already plain',
  'en-x-pirate:filters.actorKindEmail': 'this catalogue already keeps Email verbatim',
  'en-x-pirate:filters.actorLoginPlaceholder': 'an example login',
  'en-x-pirate:actorType.vcsUser': 'an initialism',
  'en-x-pirate:actorType.slackUser': 'a product name',
  'en-x-yoda:filters.actor': 'one word has no clauses to reorder',
  'en-x-yoda:filters.actorKindEmail': 'one word has no clauses to reorder',
  'en-x-yoda:filters.actorLoginPlaceholder': 'an example login',
  'en-x-yoda:actorType.vcsUser': 'an initialism',
  'en-x-yoda:actorType.system': 'one word has no clauses to reorder',
  'en-x-yoda:actorType.slackUser': 'a product name',
  'tlh:filters.actorLoginPlaceholder': 'an example login',
  'tlh:actorType.vcsUser': 'an initialism this catalogue keeps',
  'tlh:actorType.slackUser': 'a product name',
}

test('the other novelty locales transformed the new values too', () => {
  const unused = new Set(Object.keys(NOVELTY_IDENTICAL))
  let differed = 0
  for (const locale of ['en-x-marklar', 'en-x-lolcat', 'en-x-pirate', 'en-x-yoda', 'tlh']) {
    const doc = JSON.parse(readFileSync(join(MESSAGES, `${locale}.json`), 'utf8'))
    for (const key of NEW_KEYS) {
      const source = reach(en, key) as string
      const got = reach(doc, `adminAuditLog.${key}`) as string
      assert.equal(typeof got, 'string', `${locale} ${key} is missing`)
      const id = `${locale}:${key}`
      if (got === source) {
        assert.ok(
          id in NOVELTY_IDENTICAL,
          `${id} is still the English string ${JSON.stringify(source)} -- ` +
            `transform it, or name it in NOVELTY_IDENTICAL with the reason`,
        )
        unused.delete(id)
      } else {
        assert.ok(
          !(id in NOVELTY_IDENTICAL),
          `${id} now differs -- drop it from NOVELTY_IDENTICAL`,
        )
        differed++
      }
    }
  }
  assert.equal(unused.size, 0, `stale NOVELTY_IDENTICAL entries: ${[...unused].join(', ')}`)
  // 5 locales x 8 keys = 40, of which 20 are named above.
  assert.equal(differed, 20, `${differed} value(s) transformed, expected 20`)
})

test('every real locale translated the three that are not a name or an initialism', () => {
  // `actor`, `actorKind`, `actorKindEmail`, `actorKindLogin` and `system` are
  // ordinary words. A handful of locales genuinely share a word with English
  // (`Actor` in Spanish, `System` in German), which is why this names them
  // rather than demanding a difference everywhere.
  const SHARED_WORD = new Set([
    'cy:filters.actor', 'es:filters.actor', 'la:filters.actor', 'nl:filters.actor',
    'cy:actorType.system', 'da:actorType.system', 'de:actorType.system',
    'nb:actorType.system', 'pl:actorType.system', 'sv:actorType.system',
    'it:filters.actorKindEmail',
  ])
  const real = readdirSync(MESSAGES)
    .filter((f) => f.endsWith('.json'))
    .map((f) => f.replace(/\.json$/, ''))
    .filter((l) => l !== 'en' && l !== 'en-GB' && !l.startsWith('en-x-') && l !== 'tlh')
  assert.equal(real.length, 25, `expected 25 real locales, found ${real.length}`)

  const unused = new Set(SHARED_WORD)
  for (const locale of real) {
    const doc = JSON.parse(readFileSync(join(MESSAGES, `${locale}.json`), 'utf8'))
    for (const key of [
      'filters.actor',
      'filters.actorKind',
      'filters.actorKindEmail',
      'filters.actorKindLogin',
      'actorType.system',
    ]) {
      const source = reach(en, key) as string
      const got = reach(doc, `adminAuditLog.${key}`) as string
      const id = `${locale}:${key}`
      if (got === source) {
        assert.ok(
          SHARED_WORD.has(id),
          `${id} is still the English string ${JSON.stringify(source)} -- ` +
            `translate it, or name it in SHARED_WORD`,
        )
        unused.delete(id)
      } else {
        assert.ok(!SHARED_WORD.has(id), `${id} now differs -- drop it from SHARED_WORD`)
      }
    }
  }
  assert.equal(unused.size, 0, `stale SHARED_WORD entries: ${[...unused].join(', ')}`)
})
