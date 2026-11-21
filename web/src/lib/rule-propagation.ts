/**
 * The propagation report an autodiscovery-rule update returns, and the one
 * piece of rendering logic it needs.
 *
 * Lives here rather than in the admin page so the two-shape `diff` below can
 * be pinned by a unit test: a helper that absorbs "the server might send X or
 * Y" has to have BOTH shapes asserted, or the test means whichever one the
 * fixture happened to use.
 */

/**
 * A per-setting diff from `rule_propagation_service`.
 *
 * It carries TWO shapes. A scalar setting arrives as `{from, to}`. The two
 * template fields (`run-tasks`, `notification-configurations`) arrive as a
 * bare array of names, because a template spec holds secrets and only its
 * names are reportable. Rendering one shape blindly prints nothing at all for
 * the other, which is why `propagationCell` takes `unknown`.
 */
export type PropagationDiff = Record<string, { from?: unknown; to?: unknown } | string[]>

/** `meta.propagation` on the rule PATCH response. */
export type PropagationReport = {
  dry_run: boolean
  matched: number
  would_change: Array<{ id: string; name: string; diff?: PropagationDiff }>
  customised: Array<{
    id: string
    name: string
    fields: Record<string, { current?: unknown; previous?: unknown; new?: unknown }>
  }>
  unchanged: Array<{ id: string; name: string }>
  skipped_lifecycle: number
}

/**
 * One verdict value as a table cell.
 *
 * `empty` is the caller's translated placeholder, so this stays free of copy.
 * An absent value and an empty string are the same thing to an operator —
 * both mean the setting carries nothing — and an empty array likewise.
 */
export function propagationCell(v: unknown, empty: string): string {
  if (v === null || v === undefined || v === '') return empty
  if (Array.isArray(v)) return v.length ? v.join(', ') : empty
  // A boolean setting's value is a literal, not UX copy, so it is not
  // translated -- the same reason a resource address is not.
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
