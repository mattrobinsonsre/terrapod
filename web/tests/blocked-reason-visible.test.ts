/**
 * The reason a control is unavailable is RENDERED, never only hovered (#2557).
 *
 * `title` is a hover tooltip. On touch -- a first-class target in this project
 * -- it is information that does not exist, and a `disabled` control is not
 * focusable either, so keyboard and assistive-technology users do not reach it
 * through focus. So a blocked reason that lives only in a `title` is reachable
 * by exactly one input mode, which `AGENTS.md` names verbatim: "no info that
 * only appears in a `:hover` tooltip".
 *
 * The subject is DERIVED from the tree rather than listed, because the issue
 * that prompted this named one component and the sweep found four more sites in
 * four files. A hand-kept list would have been the same omission one layer up.
 *
 * What is forbidden is narrow on purpose: a `title` with a FALLBACK -- `a ? b :
 * c` or `a ?? b` -- on a control that is also `disabled`. That shape is the
 * defect, because the two branches are "the reason it is blocked" and "what it
 * would otherwise do", so the blocked branch is load-bearing and hover-only.
 * Both spellings, because the site this was raised for used the second one. An unconditional `title`
 * is supplementary prose on a control whose own label already says what it is,
 * so it is left alone; forbidding those would flag "Re-run the AI summary
 * against the same plan inputs" and teach people to silence this.
 *
 * Only lowercase tags are examined. A capitalised tag's `title` is a component
 * prop, and the three that take one (`PageHeader`, `Field`, `MobileCard`)
 * render it as visible content rather than as an attribute.
 */
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const here = path.dirname(fileURLToPath(import.meta.url))
const SRC = path.join(here, '..', 'src')

/** Every `.tsx` under `src`, walked rather than shelled out for.
 *
 * Deliberately not `grep`: this suite runs in a container whose `grep` is
 * BusyBox, which has neither `--include` nor `--exclude-dir` and exits 2, so a
 * subprocess gate dies rather than reporting. A walk has no dialect. */
function tsxFiles(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) tsxFiles(p, out)
    else if (e.name.endsWith('.tsx')) out.push(p)
  }
  return out
}

/**
 * Each JSX opening tag in `src`, from `<` to the `>` that closes it.
 *
 * Brace-aware, which is the whole difficulty: a tag routinely contains
 * `onClick={() => ...}`, so a matcher that stops at the first `>` ends the tag
 * inside an arrow function and never sees the attributes after it. The first
 * version of this sweep did exactly that and reported 7 sites where there are
 * 20 -- it hid 13, including three real defects.
 */
export function openingTags(src: string): { offset: number; tag: string }[] {
  const out: { offset: number; tag: string }[] = []
  let i = 0
  while (i < src.length) {
    const next = src[i + 1]
    if (src[i] === '<' && next && (/[A-Za-z]/.test(next) || next === '_')) {
      let j = i + 1
      let depth = 0
      let instr: string | null = null
      for (; j < src.length; j++) {
        const c = src[j]
        if (instr) {
          if (c === instr && src[j - 1] !== '\\') instr = null
        } else if (c === '"' || c === "'" || c === '`') instr = c
        else if (c === '{') depth++
        else if (c === '}') depth--
        else if (c === '>' && depth === 0) {
          out.push({ offset: i, tag: src.slice(i, j + 1) })
          break
        } else if (c === '<' && depth === 0) break
      }
      i = j + 1
    } else i++
  }
  return out
}

/**
 * The `title={...}` expression in a tag, brace-BALANCED.
 *
 * Not a regex. `/title=\{(.*?)\}\s/` reads correctly and is wrong twice: it
 * needs whitespace after the closing brace, which `title={a ? b : c}>` does not
 * have, and it stops at the first `}`, which `title={t('k', { n })}` reaches
 * early. The first costs nothing in the real tree -- where a `title=` is
 * followed by a newline -- and everything in the self-test, which is how that
 * narrowing was caught here rather than shipping as a silent zero.
 */
function titleExpr(tag: string): string | null {
  const at = tag.indexOf('title={')
  if (at === -1) return null
  let depth = 0
  for (let i = at + 6; i < tag.length; i++) {
    const c = tag[i]
    if (c === '{') depth++
    else if (c === '}') {
      depth--
      if (depth === 0) return tag.slice(at + 7, i)
    }
  }
  return null
}

/** A conditional `title` on a `disabled` lowercase tag, or null. */
export function hoverOnlyReason(tag: string): string | null {
  const name = /^<([A-Za-z_][\w.]*)/.exec(tag)?.[1]
  if (!name || !/[a-z]/.test(name[0])) return null
  if (!tag.includes('disabled')) return null
  const expr = titleExpr(tag)
  if (!expr) return null
  // BOTH spellings of a fallback, and the second one is why this is spelled out
  // rather than left as a ternary test. #2557's own component wrote it
  // `title={primary.blockedReason ?? primary.title}` -- a nullish coalesce has a
  // `?` and no `:`, so a ternary-only predicate is blind to the exact site the
  // issue was raised about. The mutation check against the pre-fix tree is what
  // found that: the gate named four of five files and skipped the one that
  // mattered most.
  const fallback = expr.includes('??') || (expr.includes('?') && expr.includes(':'))
  return fallback ? expr.split(/\s+/).join(' ') : null
}

describe('a blocked reason is never hover-only', () => {
  // The self-test runs FIRST and is the thing that stops this gate narrowing
  // silently. A count over the tree cannot do that job: a file legitimately
  // losing its last conditional title is benign, a matcher losing the
  // brace-aware scan is not, and a bare count cannot tell those apart.
  it('recognises each shape it claims to, and nothing else', () => {
    const DEFECT = [
      '<button disabled={x} title={a ? b : c}>',
      '<select disabled={editId !== null} title={editId !== null ? t(\'k\') : undefined}>',
      // the arrow function is the whole reason the scan must be brace-aware
      '<button onClick={() => go()} disabled={busy || !!r} title={r ? r : d}>',
      // the spelling #2557's own component used, and the one an earlier draft
      // of this fixture list dropped -- at which point the gate stopped seeing
      // the component the issue is about
      '<button disabled={busy || !!r} title={r ?? d}>',
      // a nested object in the call -- the other way a first-`}` matcher fails
      '<button disabled={x} title={a ? t(\'k\', { n: 1 }) : undefined}>',
    ]
    const FINE = [
      '<button disabled={busy} title={t(\'regenerate\')}>',   // unconditional prose
      '<button title={a ? b : c}>',                            // not disabled
      '<PageHeader disabled title={a ? b : c}>',               // renders visibly
      '<span title={a ? b : c}>',                              // not a control
    ]
    for (const t of DEFECT) assert.ok(hoverOnlyReason(t), `should flag: ${t}`)
    for (const t of FINE) assert.equal(hoverOnlyReason(t), null, `should allow: ${t}`)
  })

  const files = tsxFiles(SRC)

  it('examines a real tree, so it cannot pass on nothing', () => {
    assert.ok(files.length >= 80, `only ${files.length} .tsx files found`)
    const tags = files.reduce((n, f) => n + openingTags(readFileSync(f, 'utf8')).length, 0)
    assert.ok(tags >= 2000, `only ${tags} opening tags scanned`)
  })

  it('no disabled control carries its reason in a conditional title', () => {
    const bad: string[] = []
    for (const f of files) {
      const src = readFileSync(f, 'utf8')
      for (const { offset, tag } of openingTags(src)) {
        const expr = hoverOnlyReason(tag)
        if (!expr) continue
        const line = src.slice(0, offset).split('\n').length
        bad.push(`${path.relative(SRC, f)}:${line}  title={${expr.slice(0, 70)}}`)
      }
    }
    assert.deepEqual(
      bad,
      [],
      `a disabled control's reason is reachable only by hovering (#2557).\n` +
        `Render it instead -- a visible line in the control's own label, as\n` +
        `RunKindSplitButton's BlockedReason does -- and leave only\n` +
        `supplementary prose in the title:\n  ${bad.join('\n  ')}`,
    )
  })
})
