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
import { HINT_LEADS, LONG, NOT_TIERED } from '../src/lib/hint-tiers.ts'

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
