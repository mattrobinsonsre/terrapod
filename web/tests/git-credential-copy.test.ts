// The seventeen git-credential strings are translated, not merely present (#2198).
//
// `check-i18n-completeness` compares each catalogue's key SET against `en`, so
// it has no opinion about values: a locale holding the English string passes it
// while being untranslated, and thirty-one locales doing so are
// indistinguishable from thirty-one that are correct. That is what happened
// here — every one of these seventeen keys was the English source verbatim in
// all thirty-one locales that carry them, for as long as they have existed,
// with every gate green.
//
// So this file asserts the properties no key-parity gate can see:
//
//   * each of the seventeen differs from its English source, in every locale,
//     with each exemption naming the string and the reason;
//   * the field label `gitSshHint` cross-references is the one `gitKnownHosts`
//     actually carries — a hint telling the operator to look for a field that
//     does not exist in their language passes a per-key check perfectly;
//   * the technical literals an operator types survive translation;
//   * `en-x-leet` is the substitution of the source rather than a translation.
//
// Run with: npm run test:unit   (node:test, no test framework dependency)

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const en = JSON.parse(readFileSync('messages/en.json', 'utf8'))
const EN_GIT: Record<string, string> = Object.fromEntries(
  Object.entries(en.workspaceDetail.variables as Record<string, string>).filter(
    ([k, v]) => k.startsWith('git') && typeof v === 'string',
  ),
)

/** The offered locales, read from the source of truth rather than listed here. */
const offered = (() => {
  const config = readFileSync('src/i18n/config.ts', 'utf8')
  const block = config.match(/export const locales = \[([\s\S]*?)\] as const/)
  assert.ok(block, 'could not find the `locales` array in src/i18n/config.ts')
  return [...block[1].matchAll(/'([^']+)'/g)].map((m) => m[1])
})()

const catalogue = (loc: string) =>
  JSON.parse(readFileSync(`messages/${loc}.json`, 'utf8')).workspaceDetail?.variables ?? {}

/**
 * The locales a translation is expected of: everything offered except the
 * source itself, and except `en-GB` — a dialect override carrying only the
 * spelling deltas from the American source, and none of these seventeen
 * differs between British and American English, so it is expected to carry
 * none of them and is not a gap.
 */
const TRANSLATED = offered.filter((l) => l !== 'en' && l !== 'en-GB')

test('the subject is the whole git namespace, derived from the source catalogue', () => {
  // A gate that iterates a derived set passes on nothing when the set empties,
  // and passes on a SHRUNKEN set while staying above a floor. So assert the
  // membership, not just a count: a key added to or removed from the form
  // fails here and is a deliberate act.
  assert.deepEqual(
    Object.keys(EN_GIT).sort(),
    [
      'gitCredSource',
      'gitHttpHint',
      'gitKnownHosts',
      'gitPrivateKey',
      'gitRewrite',
      'gitRewriteNone',
      'gitRewriteToHttps',
      'gitRewriteToSsh',
      'gitSourceStatic',
      'gitSourceVcs',
      'gitSshHint',
      'gitToken',
      'gitUrlPattern',
      'gitUsername',
      'gitVcsConnection',
      'gitVcsConnectionHint',
      'gitVcsConnectionSelect',
    ],
    'the git credential namespace has changed — translate the new key in every locale',
  )
})

test('every offered locale carries the whole git credential namespace', () => {
  for (const loc of offered) {
    const cat = catalogue(loc)
    if (loc === 'en-GB') {
      for (const key of Object.keys(EN_GIT)) {
        assert.equal(cat[key], undefined, `en-GB should carry no delta for ${key}`)
      }
      continue
    }
    for (const key of Object.keys(EN_GIT)) {
      assert.equal(typeof cat[key], 'string', `${loc}: workspaceDetail.variables.${key} is missing`)
    }
  }
})

test('the literals an operator types survive every translation', () => {
  // Derived from the source rather than listed per key: whatever of these the
  // English contains, the translation must contain too. `git::https://` is a
  // module-source prefix the engine parses and the operator types — a
  // translated or transliterated copy breaks the thing the string explains.
  const literals = ['git::https://', 'git::ssh://', 'ssh://', 'https://', 'github.com', 'gitlab.com']
  /**
   * Count each literal's occurrences, masking the LONGER literals first.
   * `git::https://` contains `https://`, so a plain containment test is
   * satisfied for both by keeping only the prefixed form — which would let a
   * standalone scheme mention be lost in a sentence that also carries the
   * prefixed one. No key is exposed to that today (every standalone `https://`
   * in the source sits inside a `git::` prefix), so this is precision against
   * the source changing rather than a live hole.
   */
  const counts = (s: string) => {
    const out: Record<string, number> = {}
    let rest = s
    for (const lit of [...literals].sort((a, b) => b.length - a.length)) {
      const parts = rest.split(lit)
      out[lit] = parts.length - 1
      rest = parts.join('\u0000')
    }
    return out
  }
  let checked = 0
  for (const loc of TRANSLATED) {
    // `en-x-leet` substitutes every character including these, which is the
    // committed convention for that locale alone: the catalogue stores
    // `https://github.com/org/repo` as `h77p5://g17hub.c0m/0rg/r3p0`. Every
    // other novelty locale preserves the literal and transforms the prose
    // around it, which is what the rest of this assertion holds them to.
    if (loc === 'en-x-leet') continue
    const cat = catalogue(loc)
    for (const [key, source] of Object.entries(EN_GIT)) {
      const want = counts(source)
      const got = counts(cat[key])
      for (const lit of literals) {
        if (!want[lit]) continue
        checked++
        assert.ok(
          got[lit] >= want[lit],
          `${loc}: ${key} has ${got[lit]} of the literal ${lit}, the source has ${want[lit]}`,
        )
      }
    }
  }
  // The floor earns its place only if raising it fails: without it a matcher
  // that stops matching passes on nothing.
  assert.ok(checked >= 150, `expected the literals to be exercised widely, checked ${checked}`)
})

test('en-x-leet is the substitution of the source, not a translation', () => {
  // The locale is a deterministic character substitution, so it is checked
  // against the rule rather than against a snapshot of itself. Scoped to these
  // seventeen deliberately: the wider catalogue is NOT a pure function of the
  // source — measured, 74 of 3072 values deviate, and it is internally
  // inconsistent about whether a `<code>` span, an ICU sub-message or a
  // placeholder name is substituted. None of those shapes occurs in these
  // seventeen, which is asserted below, so the plain rule is exactly right
  // here and would be wrong as a global claim.
  const map: Record<string, string> = { a: '4', e: '3', i: '1', o: '0', s: '5', t: '7' }
  const leetify = (s: string) =>
    [...s].map((ch) => map[ch] || map[ch.toLowerCase()] || ch).join('')
  const cat = catalogue('en-x-leet')
  for (const [key, source] of Object.entries(EN_GIT)) {
    assert.ok(
      !/<code>|<\/?[a-zA-Z][a-zA-Z0-9]*>|\{/.test(source),
      `${key} has gained a code span, tag or ICU argument — the plain leet rule no longer applies`,
    )
    assert.equal(cat[key], leetify(source), `en-x-leet.${key} is not the substitution of the source`)
  }
})

test('every locale actually translates all seventeen, with each exemption reasoned', () => {
  // The property the completeness gate cannot see. An exemption names the
  // string and the reason, and is checked in BOTH directions: a key listed as
  // exempt that has since moved fails here too, so the list cannot quietly
  // outlive the fact that justified it.
  //
  // `gitToken` is not listed. Twelve locales legitimately render it as the
  // English word, and which twelve is DERIVED rather than written down: a
  // locale may leave it iff its own `settings.sessions.columns.token` is also
  // `Token`. That sibling is a pre-existing human translation of the same
  // noun, so the rule reads the catalogue's own settled register instead of a
  // ledger that drifts. It is also sharper than any rule about script would
  // be: `tr` and `fi` are Latin-script and translate it (`Belirteç`,
  // `Tunnus`), so they are correctly held to a translation.
  const TOKEN_SIBLING = ['settings', 'sessions', 'columns', 'token']
  const exempt: Record<string, Record<string, string>> = {
    'en-x-marklar': {
      gitRewriteNone:
        'the null option of a radio group — a quantifier, not a noun, and marklar replaces nouns only',
    },
    'en-x-yoda': {
      gitRewriteNone: 'one word; there is no clause to reorder',
      gitUsername: 'one word; there is no clause to reorder',
    },
  }
  let exemptions = 0
  for (const loc of TRANSLATED) {
    const whole = JSON.parse(readFileSync(`messages/${loc}.json`, 'utf8'))
    const cat = whole.workspaceDetail.variables
    const sibling = TOKEN_SIBLING.reduce((o, k) => (o ?? {})[k], whole)
    for (const [key, source] of Object.entries(EN_GIT)) {
      const same = cat[key] === source
      if (key === 'gitToken') {
        if (sibling === 'Token') continue // derived exemption, see above
        assert.ok(!same, `${loc}: gitToken is still "Token", but this catalogue renders the same noun as ${JSON.stringify(sibling)}`)
        continue
      }
      const reason = exempt[loc]?.[key]
      if (reason) {
        exemptions++
        assert.ok(same, `${loc}: ${key} is exempt as "${reason}" but has moved — drop the exemption`)
      } else {
        assert.ok(!same, `${loc}: ${key} is still the English string`)
      }
    }
  }
  // An exemption table that has emptied would make the bidirectional half of
  // this test vacuous without failing anything.
  assert.equal(exemptions, 3, 'the exemption table has changed — re-read the reasons')
})

test('the hint points at the field label the catalogue actually carries', () => {
  // `gitSshHint` tells the operator to "leave Known hosts blank", naming the
  // field whose own label is `gitKnownHosts` and which renders directly beneath
  // it (page.tsx: the hint at the `git_ssh_auth` panel, the label on the
  // textarea below). A per-key identity check is perfectly happy when the hint
  // names a field label that does not exist in that language, so it is checked
  // here: the label's head — everything before the qualifying parenthesis, in
  // either half- or full-width — must appear in the hint verbatim.
  //
  // A GAP, stated rather than gated: both hints also reference `gitUrlPattern`
  // ("the host / URL pattern above") and `gitHttpHint` references `gitRewrite`
  // ("with the rewrite below"). Neither is asserted, because a verbatim match
  // is wrong for them in eleven of the thirty-one locales for sound
  // grammatical reasons — the Nordic languages take the definite form in
  // running prose (`mönster` -> `mönstret`), the Slavic ones the genitive
  // (`Шаблон хоста` -> `шаблону хоста`), and yoda reorders the label itself.
  // A gate whose failures are mostly correct translations teaches people to
  // silence it, so the gap is documented here instead of enforced badly.
  const head = (s: string) => s.split(/\s*[(（]/)[0].trim()
  // Welsh mutates the initial consonant of a noun in this position: the label
  // is `Gwesteiwyr hysbys` and the hint carries `Westeiwyr hysbys` after
  // `gadewch`. That is the same phrase correctly inflected, not a different
  // one, so the exemption asserts the MUTATED form is present rather than
  // waiving the check.
  const mutated: Record<string, string> = { cy: 'Westeiwyr hysbys' }
  for (const loc of TRANSLATED) {
    const cat = catalogue(loc)
    const label = head(cat.gitKnownHosts)
    assert.ok(label.length > 0, `${loc}: gitKnownHosts has no label head`)
    const expected = mutated[loc] ?? label
    assert.ok(
      cat.gitSshHint.includes(expected),
      `${loc}: gitSshHint does not name the "${expected}" field it tells the operator to leave blank`,
    )
  }
})

test('no en-x-leet value is left as plain English', () => {
  // The same defect as the seventeen above, caught for the WHOLE catalogue
  // rather than one namespace — because for this locale alone the transform is
  // mechanical, so "is this still the English string" has an exact answer with
  // nothing to exempt. 33 values were sitting here as plain English when #2198
  // was written, most of the `runDetail.cost.*` block, invisible to every gate.
  //
  // This is deliberately NOT the stronger claim that the catalogue EQUALS the
  // mechanical substitution. Measured, 39 values differ from it legitimately:
  // a translator reached for a better leet than a character map produces
  // (`Repository` is `R3p0`, not `R3p05170ry`) and substituted inside `<code>`
  // spans and ICU sub-messages where the rule here is conservative. Asserting
  // equality would fail on all of those and teach people to delete the gate;
  // asserting "not still English" fails only on the real defect.
  const map: Record<string, string> = { a: '4', e: '3', i: '1', o: '0', s: '5', t: '7' }
  const leetify = (s: string) =>
    [...s].map((ch) => map[ch] || map[ch.toLowerCase()] || ch).join('')
  /**
   * Spans that must survive verbatim: a code span's content, a rich-text tag
   * NAME, the inventory placeholder, and any ICU construct — the last
   * brace-matched to arbitrary depth, because a plural whose sub-message
   * interpolates another argument is two deep and an under-matched span
   * leetifies the `plural`/`one`/`other` keywords, which stops the message
   * parsing at all.
   */
  const verbatim = /<code>[\s\S]*?<\/code>|<\/?[a-zA-Z][a-zA-Z0-9]*>|\$'\{'terrapod:[^'{}]*'\}'/g
  const icuSpans = (s: string): [number, number][] => {
    const out: [number, number][] = []
    let depth = 0
    let start = -1
    for (let i = 0; i < s.length; i++) {
      if (s[i] === '{') {
        if (depth === 0) start = i
        depth++
      } else if (s[i] === '}' && depth > 0) {
        depth--
        if (depth === 0) out.push([start, i + 1])
      }
    }
    return out
  }
  const leet = (s: string) => {
    const keep = [...[...s.matchAll(verbatim)].map((m) => [m.index!, m.index! + m[0].length] as [number, number]),
                  ...icuSpans(s)].sort((a, b) => a[0] - b[0])
    const merged: [number, number][] = []
    for (const [a, b] of keep) {
      const last = merged[merged.length - 1]
      if (last && a <= last[1]) last[1] = Math.max(last[1], b)
      else merged.push([a, b])
    }
    let out = ''
    let pos = 0
    for (const [a, b] of merged) {
      out += leetify(s.slice(pos, a)) + s.slice(a, b)
      pos = b
    }
    return out + leetify(s.slice(pos))
  }

  const cat = JSON.parse(readFileSync('messages/en-x-leet.json', 'utf8'))
  const offenders: string[] = []
  let changeable = 0
  const walk = (src: Record<string, unknown>, loc: Record<string, unknown>, path = '') => {
    for (const [k, v] of Object.entries(src)) {
      const p = path ? `${path}.${k}` : k
      if (v && typeof v === 'object') {
        const sub = loc?.[k]
        if (sub && typeof sub === 'object') walk(v as Record<string, unknown>, sub as Record<string, unknown>, p)
      } else if (typeof v === 'string' && typeof loc?.[k] === 'string') {
        // A value the transform cannot change — a bare argument list, a whole
        // plural message — is correctly identical and is not a defect.
        if (leet(v) === v) continue
        changeable++
        if (loc[k] === v) offenders.push(`${p}: ${JSON.stringify(v.slice(0, 60))}`)
      }
    }
  }
  walk(en as Record<string, unknown>, cat)
  assert.ok(changeable >= 2500, `expected most of the catalogue to be transformable, counted ${changeable}`)
  assert.deepEqual(offenders, [], `en-x-leet values still holding the English string:\n  ${offenders.join('\n  ')}`)
})

test('en-x-leet never leetifies an ICU argument name', () => {
  // `settings.slackLinks.confirmRevoke` held `{u53r}` against a `{user}`
  // argument, so the placeholder could not resolve — a functional defect, not
  // a style one, and the only reason it was found is that deriving the
  // transform for #2198 made the catalogue's own inconsistencies countable.
  /**
   * Argument names at ODD brace depth. ICU alternates: a placeholder opens at
   * odd depth and its first identifier is the argument name, while a plural's
   * sub-message opens at even depth and its first word is prose. A plain regex
   * conflates them and reports `{count, plural, one {Scanned # repository.}}`
   * as carrying an argument called `Scanned` — which is how my first version of
   * this gate accused the catalogue of a defect it does not have. The trailing
   * `,`/`}` requirement alone is not enough either: a one-word sub-message like
   * `{count, plural, one {workspace} …}` satisfies it.
   */
  const en_args = (s: string): string[] => {
    const out: string[] = []
    let depth = 0
    for (let i = 0; i < s.length; i++) {
      if (s[i] === '{') {
        depth++
        if (depth % 2 === 1) {
          let j = i + 1
          while (j < s.length && /\s/.test(s[j])) j++
          let k = j
          while (k < s.length && /[A-Za-z0-9_]/.test(s[k])) k++
          if (k > j && /^[,}]/.test(s.slice(k).trimStart())) out.push(s.slice(j, k))
        }
      } else if (s[i] === '}' && depth > 0) depth--
    }
    return out
  }
  const cat = JSON.parse(readFileSync('messages/en-x-leet.json', 'utf8'))
  let checked = 0
  const walk = (src: Record<string, unknown>, loc: Record<string, unknown>, path = '') => {
    for (const [k, v] of Object.entries(src)) {
      const p = path ? `${path}.${k}` : k
      if (v && typeof v === 'object') {
        const sub = loc?.[k]
        if (sub && typeof sub === 'object') walk(v as Record<string, unknown>, sub as Record<string, unknown>, p)
      } else if (typeof v === 'string' && typeof loc?.[k] === 'string') {
        const want = en_args(v)
        if (!want.length) continue
        checked++
        const got = en_args(loc[k] as string)
        for (const name of want) {
          assert.ok(got.includes(name), `en-x-leet.${p} lost the ICU name ${name} (has ${JSON.stringify(got)})`)
        }
      }
    }
  }
  walk(en as Record<string, unknown>, cat)
  assert.ok(checked >= 100, `expected many ICU-bearing strings, checked ${checked}`)
})
