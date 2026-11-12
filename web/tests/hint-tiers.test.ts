/**
 * Every long hint is two-tier, or says why not (#2413 F15).
 *
 * DERIVED from `messages/en.json`, not from a list: the subject is "every hint
 * at or over 250 characters", and a hand-kept list of those would be the same
 * omission F15 is about, one layer up. A new long hint fails here until it is
 * split or ledgered.
 *
 * The finding's own count was 15 when it was written and is 18 now, which is
 * the argument for deriving it.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { HINT_LEADS, LONG, NOT_TIERED, TITLE_EXPLANATION } from '../src/lib/hint-tiers.ts'

const here = path.dirname(fileURLToPath(import.meta.url))
const en = JSON.parse(readFileSync(path.join(here, '..', 'messages', 'en.json'), 'utf8'))

/** Every string in the catalogue, by dotted key. */
function flat(o: Record<string, unknown>, p = '', out: Record<string, string> = {}) {
  for (const [k, v] of Object.entries(o)) {
    const kk = p ? `${p}.${k}` : k
    if (v && typeof v === 'object') flat(v as Record<string, unknown>, kk, out)
    else if (typeof v === 'string') out[kk] = v
  }
  return out
}
const ALL = flat(en)

/** `HINT_LEADS` is namespace-relative, so match on the dotted suffix. */
const tiered = new Set(
  Object.keys(HINT_LEADS).flatMap((rel) =>
    Object.keys(ALL).filter((full) => full === rel || full.endsWith(`.${rel}`)),
  ),
)

describe('long hints are two-tier', () => {
  it('every string at or over the threshold is tiered or ledgered', () => {
    const long = Object.entries(ALL).filter(([, v]) => v.length >= LONG)
    const unhandled = long
      .map(([k]) => k)
      .filter((k) => !tiered.has(k) && !(k in NOT_TIERED))
    assert.deepEqual(
      unhandled,
      [],
      `these hints are >= ${LONG} chars with no lead and no ledger entry:\n  ${unhandled.join('\n  ')}`,
    )
  })

  it('the threshold actually bites, so the subject is not empty', () => {
    // A floor: if nothing is long any more, this test is asserting nothing and
    // should fail rather than pass silently.
    const long = Object.values(ALL).filter((v) => v.length >= LONG)
    assert.ok(long.length >= 10, `only ${long.length} long strings; has the threshold drifted?`)
  })

  it('every lead key exists and is SHORTER than its hint', () => {
    // The point of a lead is that it is short. A lead longer than its hint
    // would satisfy the registry and defeat the finding.
    for (const [hint, lead] of Object.entries(HINT_LEADS)) {
      const hk = Object.keys(ALL).find((f) => f === hint || f.endsWith(`.${hint}`))
      assert.ok(hk, `hint key not in en.json: ${hint}`)
      const lk = hk.replace(hint, lead)
      assert.ok(lk in ALL, `lead key not in en.json: ${lk}`)
      assert.ok(
        ALL[lk].length < ALL[hk].length,
        `${lk} (${ALL[lk].length}) is not shorter than ${hk} (${ALL[hk].length})`,
      )
    }
  })

  it('a lead is short enough to be worth reading at a glance', () => {
    // Not a style rule for its own sake: a 400-character "lead" is the wall of
    // text with an icon added, which is the failure mode this invites.
    for (const [hint, lead] of Object.entries(HINT_LEADS)) {
      const hk = Object.keys(ALL).find((f) => f === hint || f.endsWith(`.${hint}`))!
      const lk = hk.replace(hint, lead)
      assert.ok(ALL[lk].length < LONG, `${lk} is ${ALL[lk].length} chars, itself a long hint`)
    }
  })

  it('every ledger entry carries a reason', () => {
    // The recorded rule for any exemption dict: the half that is checkable gets
    // checked. An entry with an empty reason is an entry nobody has justified.
    for (const [k, why] of Object.entries(NOT_TIERED)) {
      assert.ok(why.trim().length > 15, `${k} has no real reason: ${why}`)
      assert.ok(k in ALL, `ledgered key is not in en.json at all: ${k}`)
    }
  })

  it('the ledger holds nothing that is no longer long', () => {
    // A stale exemption is how a ledger stops describing the code. If a string
    // has been shortened, its entry has to go.
    for (const k of Object.keys(NOT_TIERED)) {
      assert.ok(
        ALL[k].length >= LONG,
        `${k} is now ${ALL[k].length} chars; remove its NOT_TIERED entry`,
      )
    }
  })
})

describe('the lead rules hold in EVERY locale, not just English', () => {
  // The two length rules above read `en.json` alone, so a 400-character lead in
  // a translation satisfied nothing -- and the languages that expand 20-30%
  // against English are exactly where it would happen. The rules are about what
  // a reader sees, and a reader sees their own language.
  //
  // Each locale is judged on the values it ACTUALLY carries. An absent key is
  // not a failure here: the catalogues deep-merge over `en`, so an absent lead
  // renders the English one, which the rules above already cover. That is also
  // what keeps `en-GB` -- a spelling-delta override that is deliberately a
  // subset -- from failing a rule it is not in scope for.
  const localeFiles = readdirSync(path.join(here, '..', 'messages'))
    .filter((f) => f.endsWith('.json') && f !== 'en.json')
  const locales = localeFiles.map((f) => [
    f.replace(/\.json$/, ''),
    flat(JSON.parse(readFileSync(path.join(here, '..', 'messages', f), 'utf8'))),
  ] as const)

  /** The (lead, hint) key pairs, resolved once against the English catalogue. */
  const pairs = Object.entries(HINT_LEADS).map(([hint, lead]) => {
    const hk = Object.keys(ALL).find((f) => f === hint || f.endsWith(`.${hint}`))!
    return [hk.replace(hint, lead), hk] as const
  })

  it('examines a real set of locales and pairs, so it cannot pass on nothing', () => {
    // Both floors, because every assertion below is inside a loop: an empty
    // locale list or an empty pair list would report success having checked
    // nothing at all.
    assert.ok(locales.length >= 25, `only ${locales.length} locales found`)
    assert.ok(pairs.length >= 1, 'no hint/lead pairs to check')
  })

  it('a translated lead is shorter than its own translated hint', () => {
    for (const [loc, msgs] of locales) {
      for (const [lk, hk] of pairs) {
        if (!(lk in msgs) || !(hk in msgs)) continue
        assert.ok(
          msgs[lk].length < msgs[hk].length,
          `${loc}: ${lk} (${msgs[lk].length}) is not shorter than ${hk} (${msgs[hk].length})`,
        )
      }
    }
  })

  // Expressed as a multiple of LONG so it tracks if the threshold moves, and
  // generous on purpose. A translation is NOT held to the English bound: the
  // longest English lead is 220 of a 250 budget, so a faithful rendering in a
  // language that expands 20-30% lands near 290 -- measured, pt-BR 291, fr 288,
  // de 272 on `allowForkPrPlans.lead`, which is the only key that exceeds 250 in
  // any locale. Holding those to 250 would make a translator DROP CONTENT rather
  // than shorten, which is worse than a slightly long lead.
  //
  // What the rule is actually about is "a 400-character lead is the wall of text
  // with an icon added", and 375 still catches that. Measured headroom today: the
  // worst value is 291, and nothing anywhere exceeds 300.
  const LOCALE_LONG = Math.round(LONG * 1.5)

  it('a translated lead is still short enough to read at a glance', () => {
    for (const [loc, msgs] of locales) {
      for (const [lk] of pairs) {
        if (!(lk in msgs)) continue
        assert.ok(
          msgs[lk].length < LOCALE_LONG,
          `${loc}: ${lk} is ${msgs[lk].length} chars (limit ${LOCALE_LONG}), itself a long hint`,
        )
      }
    }
  })

  it('that allowance is not so wide it has stopped meaning anything', () => {
    // A bound nothing approaches is a bound nobody would notice drifting. If the
    // real worst case ever climbs past two thirds of the allowance, the English
    // lead it comes from has itself grown too long for translation.
    const longest = Math.max(
      ...locales.flatMap(([, msgs]) => pairs.filter(([lk]) => lk in msgs).map(([lk]) => msgs[lk].length)),
    )
    assert.ok(
      longest < LOCALE_LONG * 0.8,
      `the longest translated lead is ${longest} of ${LOCALE_LONG}; shorten the English lead it renders`,
    )
  })
})

describe('every long hint is RENDERED two-tier, not merely registered', () => {
  // The registry half alone is satisfiable while the finding persists: a hint
  // can have a lead and still be rendered as a wall of text. So this walks the
  // sources and requires every render of a registered key to go through
  // `tieredHint` or `HintWithDetail`.
  //
  // It is the half that would otherwise be missed, because the other test goes
  // green the moment a lead exists.

  /** The registry itself, by RESOLVED path (#2425 §2.4).
   *
   *  It was `file.includes('hint-tiers')`, which matches the whole path -- so
   *  anything under a directory whose name contained that string was silently
   *  exempt. A substring over a path is never the check; the file is one file
   *  and can be named. */
  const REGISTRY = path.resolve(here, '..', 'src', 'lib', 'hint-tiers.ts')

  const SRC = (() => {
    const out: Record<string, string> = {}
    const walk = (dir: string) => {
      for (const e of readdirSync(dir, { withFileTypes: true })) {
        const p = path.join(dir, e.name)
        if (e.isDirectory()) walk(p)
        // `.ts` as well as `.tsx` (#2425 §2.4). A render needs JSX, but a plain
        // `.ts` helper can perfectly well return `t(key)` as a string for a
        // component to place -- and that would have been outside this walk
        // entirely. Widening it is also what makes the two exemptions below
        // load-bearing rather than inert: the registry is a `.ts` file.
        else if (e.name.endsWith('.tsx') || e.name.endsWith('.ts')) out[p] = readFileSync(p, 'utf8')
      }
    }
    walk(path.join(here, '..', 'src'))
    return out
  })()

  /** The names, per file, of local helpers that themselves delegate to
   *  `tieredHint` -- so passing a key to one IS a two-tier render.
   *
   *  DERIVED from each declaration's own body rather than listed: a helper is
   *  accepted only while it still reaches `tieredHint`, so one that stops
   *  delegating stops being accepted and the sites that use it light up. A
   *  hand-kept list of blessed wrapper names would be the ledger-with-no-gate
   *  shape this file exists to avoid.
   *
   *  The body runs to the next declaration boundary -- a `const`, `function`
   *  or doc comment at the same or lower indent -- rather than a window of N
   *  characters, so there is no threshold to tune. */
  const delegatingHelpers = (src: string): string[] => {
    const out: string[] = []
    for (const m of src.matchAll(/^([ \t]*)(?:const|function) (\w+)\s*[=(]/gm)) {
      const [, indent, name] = m
      const rest = src.slice(m.index! + m[0].length)
      const boundary = rest.search(
        new RegExp(`\\n {0,${indent.length}}(?:const |function |export |/\\*\\*|\\})`),
      )
      const body = boundary === -1 ? rest : rest.slice(0, boundary)
      if (/tieredHint\(|<HintWithDetail/.test(body)) out.push(name)
    }
    return out
  }

  it('no long hint key is read outside a two-tier call', () => {
    const offenders: string[] = []
    for (const [file, src] of Object.entries(SRC)) {
      const lines = src.split('\n')
      const helpers = delegatingHelpers(src)
      lines.forEach((line, i) => {
        for (const key of Object.keys(HINT_LEADS)) {
          if (!line.includes(`'${key}'`)) continue
          // A map ENTRY is not a render -- the surface's own resolver reads the
          // map and calls `tieredHint`, which is where the tiering happens.
          //
          // The character class used to be `[a-z0-9-]+`, which matches no real
          // key: every one of them carries a dot and a capital
          // (`fields.oidcAudiencesHint`). So this clause was inert, and the
          // registry was being carried by the path substring below instead.
          if (/^\s*'?[A-Za-z0-9_.-]+'?:\s*'/.test(line)) continue
          // The registry, by resolved path rather than by substring.
          if (path.resolve(file) === REGISTRY) continue
          // No WINDOW (#2425 §2.4). It was "any `tieredHint(` within six lines
          // above", so an untiered render placed below an unrelated tiered one
          // passed -- and this file derives a declaration boundary for its other
          // clause specifically to avoid a threshold, saying so in a comment.
          // This was the one place the threshold survived.
          //
          // Measured before removing it rather than argued: at window 6, 2, 1
          // and 0 the offender set is IDENTICAL, and the single site that needs
          // more than its own line is covered by `delegatingHelpers` below --
          // which reads each declaration's body to a real boundary. So the
          // window was buying nothing and hiding something.
          const tiered =
            /tieredHint\(|HintWithDetail|detail=\{/.test(line) ||
            helpers.some((h) => line.includes(`${h}(`))
          if (!tiered) {
            offenders.push(`${path.relative(path.join(here, '..'), file)}:${i + 1}  ${key}`)
          }
        }
      })
    }
    assert.deepEqual(
      offenders,
      [],
      `these render a long hint without its lead:\n  ${offenders.join('\n  ')}`,
    )
  })

  /** Every `t('…')` key inside a native `title=` attribute, with its file and
   *  line.
   *
   *  NATIVE means the attribute sits on a lowercase JSX element. A `title=` on
   *  a Capitalised name is a component PROP -- `<PageHeader title=…>` renders a
   *  heading, not a browser tooltip -- and conflating the two would make this
   *  clause fire on every page's own title.
   *
   *  The attribute's expression is read by BALANCING braces rather than by a
   *  regex ending at the first `}`, because the real tree writes
   *  `title={cond ? t('a') : undefined}` and a first-`}` reader would see
   *  neither key. It then collects every key in that expression, so a
   *  conditional with a long arm and a short one is caught on the long arm. */
  const nativeTitleKeys = (src: string): { key: string; line: number }[] => {
    const out: { key: string; line: number }[] = []
    for (const m of src.matchAll(/\btitle=\{/g)) {
      // The nearest preceding tag open decides native vs prop.
      let tag: string | null = null
      for (const om of src.slice(0, m.index!).matchAll(/<([A-Za-z][A-Za-z0-9.]*)/g)) tag = om[1]
      if (!tag || !/^[a-z]/.test(tag)) continue
      let depth = 1
      let i = m.index! + m[0].length
      for (; i < src.length && depth > 0; i++) {
        if (src[i] === '{') depth++
        else if (src[i] === '}') depth--
      }
      const expr = src.slice(m.index! + m[0].length, i - 1)
      const line = src.slice(0, m.index!).split('\n').length
      for (const km of expr.matchAll(/\bt[A-Za-z]*\(\s*'([A-Za-z0-9_.]+)'/g)) {
        out.push({ key: km[1], line })
      }
    }
    return out
  }

  // The matcher is self-tested on literals BEFORE it is pointed at the tree,
  // because the thing it can get wrong -- native versus prop -- produces a
  // silently smaller subject rather than an error (#2406). A count over the
  // tree cannot distinguish "no offenders" from "matched nothing".
  it('the native-versus-prop matcher classifies correctly', () => {
    const cases: [string, string[]][] = [
      ["<span title={t('a.b')}>x</span>", ['a.b']],
      ["<input\n  title={t('a.b')}\n/>", ['a.b']],
      ["<button title={cond ? t('a.b') : t('c.d')}>x</button>", ['a.b', 'c.d']],
      ["<label title={tWs('a.b')}>x</label>", ['a.b']],
      // A component prop, including a namespaced one -- not a tooltip.
      ["<PageHeader title={t('a.b')} />", []],
      ["<Foo.Bar title={t('a.b')} />", []],
      // Nested: the outer div is native, the inner component is NOT -- so only
      // the div's key is collected. This fixture was written the other way and
      // the self-test caught it, which is the whole argument for having one.
      ["<div title={t('a.b')}><PageHeader title={t('c.d')} /></div>", ['a.b']],
      // A literal carries no key to resolve, and is not this clause's subject.
      ['<span title="plain">x</span>', []],
      // No enclosing tag at all.
      ["const x = { title: t('a.b') }", []],
    ]
    for (const [source, want] of cases) {
      const got = nativeTitleKeys(source).map((r) => r.key)
      assert.deepEqual(got, want, `classified ${JSON.stringify(source)} as ${JSON.stringify(got)}`)
    }
  })

  it('no explanation is delivered as a hover-only native title=', () => {
    // F15 committed to migrating the native `title=` explanations and to
    // adding "no new `title=` for explanation meanwhile" -- with no artifact
    // and no gate, which is how a commitment made in a closing comment stops
    // existing. This is the gate.
    //
    // The threshold is `TITLE_EXPLANATION`, not the registry's `LONG`, and it
    // is LOWER. It was `LONG` when this clause was written, which made it
    // forbid only a hover longer than anything anyone had written: 66 keys
    // resolved, the longest was 198, and the threshold was 250 -- so the gate
    // could not fail. #2432 migrated the eight that were over 120 and lowered
    // the bar to match, which is what makes this bite. Measured after the
    // migration: every resolving native title= is under it.
    //
    // The asymmetry with `LONG` is deliberate and is argued where the constant
    // is declared: the same words are worse in a hover than on screen, so the
    // length at which a hover stops being acceptable is lower than the length
    // at which an on-screen hint wants a disclosure.
    //
    // A hover tooltip is invisible on touch, which is why the length matters:
    // a short label read aloud by a screen reader is a label, and a paragraph
    // that only appears on hover is information a phone user cannot reach.
    const offenders: string[] = []
    let examined = 0
    for (const [file, src] of Object.entries(SRC)) {
      for (const { key, line } of nativeTitleKeys(src)) {
        examined++
        // The call is namespace-relative, so resolve on the dotted suffix the
        // same way the lead lookup above does. Several keys can share a
        // suffix; every match is reported, so the message names what to look
        // at rather than asserting which one it was.
        const matches = Object.keys(ALL).filter((f) => f === key || f.endsWith(`.${key}`))
        for (const full of matches) {
          if (ALL[full].length >= TITLE_EXPLANATION) {
            offenders.push(
              `${path.relative(path.join(here, '..'), file)}:${line}  ${full} ` +
                `(${ALL[full].length} chars)`,
            )
          }
        }
      }
    }
    assert.ok(
      examined >= 50,
      `only ${examined} native title= keys examined; the matcher has stopped matching`,
    )
    assert.deepEqual(
      offenders,
      [],
      `an explanation at or over ${TITLE_EXPLANATION} chars is delivered as a native title=, ` +
        `which a touch user cannot reach. Use tieredHint instead:\n  ${offenders.join('\n  ')}`,
    )
  })

  it('and the walk actually sees the sources, so a pass means something', () => {
    // A floor: a matcher that stops matching must fail rather than pass on
    // nothing, which is this repository's most repeated gate defect.
    assert.ok(Object.keys(SRC).length >= 50, `only ${Object.keys(SRC).length} .tsx files found`)
    const anyKey = Object.keys(HINT_LEADS)[0]
    assert.ok(
      Object.values(SRC).some((s) => s.includes(`'${anyKey}'`)),
      `no source mentions ${anyKey}; the walk is looking in the wrong place`,
    )
  })
})
