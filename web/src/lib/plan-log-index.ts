/**
 * An index of the resource changes a plan log announces (#1590).
 *
 * A plan log for a workspace of any size is a long scroll, so the viewer offers
 * a picker: every resource change in log order, plus the summary. This module
 * is the parser behind it — a pure function from log text to entries, so it can
 * be tested on its own against real `terraform` and `tofu` output.
 *
 * Two things it must get right, both of which a hand-written fixture hides:
 *
 * - **Colour.** The viewer renders the log with its ANSI escapes intact, and
 *   both engines colourise these lines, so every pattern is matched against
 *   stripped text rather than the raw line.
 * - **Phrasing.** The engines agree on the common cases and diverge at the
 *   edges (`will be updated in-place` against `will be updated in place`, and
 *   the several ways a replacement is announced), so the patterns accept both
 *   rather than assuming whichever engine was to hand.
 *
 * A line that does not parse is simply left out. Nothing here throws.
 */

/** What the plan says will happen to one resource, or what went wrong. */
export type PlanEntryAction =
  | 'create'
  | 'update'
  | 'replace'
  | 'destroy'
  | 'read'
  | 'move'
  | 'import'
  | 'error'
  | 'warning'
  | 'summary'

/** One entry in the index: a line to jump to, and what it announces. */
export interface PlanLogEntry {
  /** 0-based index of the line in the log, for anchoring and scrolling. */
  line: number
  action: PlanEntryAction
  /** The resource address, or '' for the summary entry. */
  address: string
}

/**
 * ANSI escapes, as the engines emit them. CSI sequences only: neither engine
 * emits OSC in plan output, and matching those too would risk eating real text.
 */
const ANSI = /\x1b\[[0-9;]*[a-zA-Z]/g

/** The log with its ANSI escapes removed. */
export function stripAnsi(text: string): string {
  return text.replace(ANSI, '')
}

/**
 * Terraform and OpenTofu both announce a resource change as a comment line
 * naming the address, then the diff beneath it. The address is captured
 * non-greedily so a quoted `for_each` key containing spaces or dots survives.
 *
 * Order matters: the replacement forms are tried before the plain ones because
 * "is tainted, so must be replaced" also ends in a phrase the others match.
 */
const PATTERNS: ReadonlyArray<readonly [RegExp, PlanEntryAction]> = [
  [/^\s*#\s+(.+?)\s+is tainted, so must be replaced\s*$/, 'replace'],
  [/^\s*#\s+(.+?)\s+must be replaced\s*$/, 'replace'],
  [/^\s*#\s+(.+?)\s+will be replaced(?:,.*)?\s*$/, 'replace'],
  [/^\s*#\s+(.+?)\s+will be created\s*$/, 'create'],
  [/^\s*#\s+(.+?)\s+will be updated in[- ]place\s*$/, 'update'],
  [/^\s*#\s+(.+?)\s+will be destroyed\s*$/, 'destroy'],
  [/^\s*#\s+(.+?)\s+will be read during apply\s*$/, 'read'],
  [/^\s*#\s+(.+?)\s+has been deleted\s*$/, 'destroy'],
  [/^\s*#\s+(.+?)\s+will be imported\s*$/, 'import'],
  [/^\s*#\s+(.+?)\s+has moved to\s+.+?\s*$/, 'move'],
]

/**
 * A diagnostic, which is the other thing worth jumping to: a plan that failed
 * is exactly when the log is long and the reason is buried.
 *
 * Both engines print these inside a box-drawing frame, so once ANSI is gone the
 * line reads `│ Error: <title>`. The gutter is optional because `-no-color`
 * output omits the frame entirely, and the runner's log carries it — so both
 * spellings are accepted rather than assuming whichever was to hand.
 *
 * The title alone is captured. The detail beneath it is several lines of prose
 * and source excerpt, which belongs in the log, not in a picker.
 */
const DIAGNOSTIC = /^\s*(?:[│|]\s*)?(Error|Warning):\s+(.+?)\s*$/

/**
 * The closing tally, or its no-op equivalent. Both engines print one of these
 * once, near the end — and only after the run finishes, so the index gains this
 * entry a moment after the others.
 */
const SUMMARY = [
  /^Plan:\s+\d+\s+to add,\s+\d+\s+to change,\s+\d+\s+to destroy\./,
  /^No changes\./,
  /^Changes to Outputs:/,
]

/**
 * The entries a plan log announces, in log order.
 *
 * `text` is the log as the viewer holds it: ANSI escapes and all, already free
 * of the STX/ETX framing bytes. Anything unrecognised is skipped, so a log that
 * is still streaming simply yields fewer entries and grows as it arrives.
 */
export function parsePlanLogIndex(text: string | null | undefined): PlanLogEntry[] {
  if (!text) return []
  const entries: PlanLogEntry[] = []
  const lines = text.split('\n')
  let summarised = false

  for (let line = 0; line < lines.length; line++) {
    const clean = stripAnsi(lines[line])

    let matched = false
    for (const [pattern, action] of PATTERNS) {
      const m = pattern.exec(clean)
      if (m) {
        entries.push({ line, action, address: m[1].trim() })
        matched = true
        break
      }
    }
    if (matched) continue

    // A diagnostic. Checked before the summary because a failed plan prints no
    // tally at all, and after the resource patterns because a resource line
    // never contains one.
    const diag = DIAGNOSTIC.exec(clean)
    if (diag) {
      entries.push({
        line,
        action: diag[1] === 'Error' ? 'error' : 'warning',
        address: diag[2].trim(),
      })
      continue
    }

    // Only the first summary line counts: a destroy plan prints the tally
    // again in the apply phase, and the two logs can share a viewer.
    if (!summarised && SUMMARY.some((p) => p.test(clean.trimStart()))) {
      entries.push({ line, action: 'summary', address: '' })
      summarised = true
    }
  }

  return entries
}
