// Pure helpers for the workspace-list filter input.
//
// The input is a single text box that mixes name substrings, label predicates,
// and status, flag and lock predicates separated by whitespace. Each token is
// either:
//
//   - bare word          → name substring match (case-insensitive)
//   - "status:value"     → the latest run's own status, exact match
//   - "flag:value"       → a health condition (see below)
//   - "locked:true|false" → the manual state lock
//   - "key:value"        → label predicate (case-sensitive exact match)
//   - "key=value"        → same as key:value
//   - "key:" / "key="    → label key-only predicate (any value, including empty)
//
// How terms combine. Terms intersect (AND), with one exception: the two
// multi-select dropdowns, Status and Flags, union their own selections.
//
//   - `status:` is ONLY the run's status. A workspace has exactly one, so two
//     `status:` terms can only mean "either": `status:planned status:applied`
//     is planned OR applied.
//   - `flag:` is one health condition each (`flag:drifted`, `flag:vcs-error`),
//     derived from the server's `health-conditions` array. A workspace can
//     carry several at once, and `flag:a flag:b` is "either".
//   - `locked:true` / `locked:false` is its own key, not a flag: a lock is a
//     deliberate state rather than something wrong. Keeping it out of the
//     union is what makes `locked:true flag:drifted` mean locked AND drifted.
//   - Everything else intersects: name terms, label terms (so `env:prod
//     env:staging` still matches nothing), and each group against the others.
//
// The old `status:locked`, `status:unhealthy` and workspace-level spellings are
// carried onto `locked:` and `flag:` on load by `migrateLegacyFilter`.
//
// `flag:` reads the server's `health-conditions`, not the pill, which hides a
// condition behind `needs-confirm` and shows drift without checking detection.
//
// `status`, `flag` and `locked` are the keys THIS PARSER special-cases, so a
// literal label of any of those names would make the filter ambiguous; all three
// are reserved in `RESERVED_LABEL_KEYS` in
// `services/terrapod/services/label_validation.py`. `owner` is also reserved,
// but for an unrelated reason (it maps to `owner_email`, which grants workspace
// admin); it parses here as an ordinary label term. See `docs/rbac.md`
// § Reserved label keys.

import { RUN_STATUSES } from './workspace-status.ts'

export interface NameTerm {
  kind: 'name'
  value: string
}

export interface LabelTerm {
  kind: 'label'
  key: string
  value: string | null // null = any-value match
}

export interface StatusTerm {
  kind: 'status'
  value: string
}

export interface FlagTerm {
  kind: 'flag'
  value: string
}

export interface LockedTerm {
  kind: 'locked'
  value: string
}

/** The health-condition codes the server emits in `health-conditions`
 *  (`_compute_health_conditions` in `tfe_v2.py`). The single list: the
 *  `flag:` values, the Flags dropdown and the Health Issues expansion all
 *  derive from it, so none can drift from the others. */
export const FLAG_CODES: readonly string[] = [
  'state_diverged',
  'no_agent_pool',
  'no_live_agent_pool',
  'vcs_error',
  'run_errored',
  'drifted',
  'drift_errored',
]

/** `state_diverged` -> `state-diverged`. */
export const flagValueForCode = (code: string): string => code.replaceAll('_', '-')

/** The `flag:` values, in `FLAG_CODES` order: how the Flags dropdown and the
 *  Health Issues expansion list them. */
export const FLAG_VALUES: readonly string[] = FLAG_CODES.map(flagValueForCode)

export type FilterTerm = NameTerm | LabelTerm | StatusTerm | FlagTerm | LockedTerm

export interface ParsedFilter {
  terms: FilterTerm[]
}

const SEPARATOR = /[:=]/

const RUN_STATUS_VALUES: ReadonlySet<string> = new Set(RUN_STATUSES.map(s => s.filter))

export function parseFilterQuery(input: string): ParsedFilter {
  const terms: FilterTerm[] = []
  for (const raw of input.split(/\s+/)) {
    const token = raw.trim()
    if (!token) continue
    const sep = token.search(SEPARATOR)
    if (sep < 0) {
      terms.push({ kind: 'name', value: token })
      continue
    }
    const key = token.slice(0, sep).trim()
    if (!key) continue // ":foo" or "=foo" with no key — skip
    const rest = token.slice(sep + 1)
    if (key === 'status' || key === 'flag' || key === 'locked') {
      // Virtual-only — `status:` with no value would be ambiguous (no workspace
      // has a "label key" called status), so drop empty. Same for flag and locked.
      if (rest === '') continue
      terms.push({ kind: key, value: rest })
      continue
    }
    terms.push({ kind: 'label', key, value: rest === '' ? null : rest })
  }
  return { terms }
}

// Spellings `status:` used to accept, and the terms that now carry them.
const LEGACY_STATUS_TERMS: ReadonlyMap<string, readonly string[]> = new Map([
  ['locked', ['locked:true']],
  ['unhealthy', FLAG_VALUES.map(v => `flag:${v}`)],
  ['drifted', ['flag:drifted']],
  ['state-diverged', ['flag:state-diverged']],
  ['vcs-error', ['flag:vcs-error']],
])

const termKey = (t: FilterTerm): string | null =>
  t.kind === 'flag' || t.kind === 'locked' ? `${t.kind}:${t.value}` : null

/** Rewrite the removed `status:` spellings into `locked:` and `flag:` terms,
 *  keeping other tokens and terms already present; returns `text` if none. */
export function migrateLegacyFilter(text: string): string {
  const parts = text.split(/(\s+)/) // tokens at even indices, whitespace between
  const have = new Set<string>()
  for (const t of parseFilterQuery(text).terms) {
    const k = termKey(t)
    if (k) have.add(k)
  }
  let changed = false
  for (let i = 0; i < parts.length; i += 2) {
    const token = parts[i]
    const sep = token.search(SEPARATOR)
    if (sep < 0 || token.slice(0, sep) !== 'status') continue
    const replacement = LEGACY_STATUS_TERMS.get(token.slice(sep + 1))
    if (!replacement) continue
    const fresh = replacement.filter(k => !have.has(k))
    fresh.forEach(k => have.add(k))
    changed = true
    if (fresh.length > 0) {
      parts[i] = fresh.join(' ')
    } else {
      parts[i] = ''
      if (i + 1 < parts.length) parts[i + 1] = ''
      else {
        // Last token: drop the nearest whitespace still left before it.
        for (let j = i - 1; j > 0; j -= 2) {
          if (parts[j] !== '') {
            parts[j] = ''
            break
          }
        }
      }
    }
  }
  return changed ? parts.join('') : text
}

export function serializeFilter(parsed: ParsedFilter): string {
  return parsed.terms
    .map(t => {
      if (t.kind === 'name') return t.value
      if (t.kind === 'status' || t.kind === 'flag' || t.kind === 'locked') return `${t.kind}:${t.value}`
      return t.value === null ? `${t.key}:` : `${t.key}:${t.value}`
    })
    .join(' ')
}

export function removeTerm(parsed: ParsedFilter, index: number): ParsedFilter {
  return { terms: parsed.terms.filter((_, i) => i !== index) }
}

/** True if the parsed filter already contains a `status:` term equal to `value`. */
export function hasStatusTerm(parsed: ParsedFilter, value: string): boolean {
  return parsed.terms.some(t => t.kind === 'status' && t.value === value)
}

/** Toggle a `status:value` term: remove it if present, append otherwise.
 *
 * Used by the preset filter buttons so a second click clears the filter
 * the button just applied — standard pill-toggle behaviour.
 */
export function toggleStatusTerm(parsed: ParsedFilter, value: string): ParsedFilter {
  if (hasStatusTerm(parsed, value)) {
    return { terms: parsed.terms.filter(t => !(t.kind === 'status' && t.value === value)) }
  }
  return { terms: [...parsed.terms, { kind: 'status', value }] }
}

/** True if the parsed filter already contains a `flag:` term equal to `value`. */
export function hasFlagTerm(parsed: ParsedFilter, value: string): boolean {
  return parsed.terms.some(t => t.kind === 'flag' && t.value === value)
}

/** Toggle a `flag:value` term: remove it if present, append otherwise. */
export function toggleFlagTerm(parsed: ParsedFilter, value: string): ParsedFilter {
  if (hasFlagTerm(parsed, value)) {
    return { terms: parsed.terms.filter(t => !(t.kind === 'flag' && t.value === value)) }
  }
  return { terms: [...parsed.terms, { kind: 'flag', value }] }
}

/** Add every health-condition flag, or remove them all when they are all
 *  present. The Health Issues chip uses this so the filter bar shows what
 *  "unhealthy" means, and a condition can be deleted from it. */
export function toggleAllFlags(parsed: ParsedFilter): ParsedFilter {
  const present = FLAG_VALUES.filter(v => hasFlagTerm(parsed, v))
  if (present.length === FLAG_VALUES.length) {
    return { terms: parsed.terms.filter(t => t.kind !== 'flag') }
  }
  const missing = FLAG_VALUES.filter(v => !hasFlagTerm(parsed, v))
  return { terms: [...parsed.terms, ...missing.map(value => ({ kind: 'flag' as const, value }))] }
}

/** True if the filter has a `locked:` term, whatever its value. */
export function hasLockedTerm(parsed: ParsedFilter): boolean {
  return parsed.terms.some(t => t.kind === 'locked')
}

/** Toggle `locked:true`: remove any `locked:` term if present, else add it. */
export function toggleLockedTerm(parsed: ParsedFilter): ParsedFilter {
  if (hasLockedTerm(parsed)) {
    return { terms: parsed.terms.filter(t => t.kind !== 'locked') }
  }
  return { terms: [...parsed.terms, { kind: 'locked', value: 'true' }] }
}

/** True if the parsed filter already contains a `key:value` label term. */
export function hasLabelTerm(parsed: ParsedFilter, key: string, value: string): boolean {
  return parsed.terms.some(t => t.kind === 'label' && t.key === key && t.value === value)
}

/** Toggle a `key:value` label term: remove it if present, append otherwise.
 *
 * Used by the inline label chips on each workspace row and by the
 * "+ Label" picker — click an existing filter chip → removes it, click
 * a new label → adds it. Symmetric with toggleStatusTerm.
 */
export function toggleLabelTerm(parsed: ParsedFilter, key: string, value: string): ParsedFilter {
  if (hasLabelTerm(parsed, key, value)) {
    return {
      terms: parsed.terms.filter(t => !(t.kind === 'label' && t.key === key && t.value === value)),
    }
  }
  return { terms: [...parsed.terms, { kind: 'label', key, value }] }
}

/** How many DISTINCT values of `kind` the filter holds that `offered` lists:
 *  what a dropdown ticks, so its badge never counts a term with no row. */
export function countOfferedTerms(
  parsed: ParsedFilter,
  kind: 'status' | 'flag',
  offered: readonly string[],
): number {
  const seen = new Set<string>()
  for (const t of parsed.terms) {
    if (t.kind === kind && offered.includes(t.value)) seen.add(t.value)
  }
  return seen.size
}

export interface MatchableWorkspace {
  attributes: {
    name: string
    labels?: Record<string, string> | null
    // Server-computed health conditions (see `_compute_health_conditions`),
    // each `{ code, ... }`. Only consulted by `flag:` terms; safe to omit.
    'health-conditions'?: { code?: string }[] | unknown[] | null
    // Manual state lock. Only consulted by `locked:` terms.
    locked?: boolean | null
  }
}

function conditionCodes(ws: MatchableWorkspace): Set<string> {
  const out = new Set<string>()
  for (const c of ws.attributes['health-conditions'] || []) {
    const code = (c as { code?: string } | null)?.code
    if (code) out.add(code)
  }
  return out
}

/** @param runStatus  The latest run's own status (`runStatusOf`), not the pill's;
 *  only `status:` terms read it, and undefined (no run) matches none. */
export function matchWorkspace(
  ws: MatchableWorkspace,
  parsed: ParsedFilter,
  runStatus?: string,
): boolean {
  const name = ws.attributes.name.toLowerCase()
  const labels = ws.attributes.labels || {}

  const statuses: string[] = []
  const flags: string[] = []
  for (const term of parsed.terms) {
    if (term.kind === 'name') {
      if (!name.includes(term.value.toLowerCase())) return false
    } else if (term.kind === 'status') {
      statuses.push(term.value)
    } else if (term.kind === 'flag') {
      flags.push(term.value)
    } else if (term.kind === 'locked') {
      // Only the two literal values match; anything else matches nothing,
      // rather than silently meaning "any".
      if (term.value !== 'true' && term.value !== 'false') return false
      if (!!ws.attributes.locked !== (term.value === 'true')) return false
    } else if (term.value === null) {
      if (!Object.prototype.hasOwnProperty.call(labels, term.key)) return false
    } else if (labels[term.key] !== term.value) {
      return false
    }
  }

  // The two dropdown groups union within themselves and intersect with the rest;
  // an unrecognised value in either matches nothing rather than dropping out.
  if (statuses.length > 0) {
    if (statuses.some(v => !RUN_STATUS_VALUES.has(v))) return false
    if (!statuses.some(v => v === runStatus)) return false
  }
  if (flags.length > 0) {
    const codes = conditionCodes(ws)
    if (flags.some(v => !FLAG_VALUES.includes(v))) return false
    if (!flags.some(v => FLAG_CODES.some(c => flagValueForCode(c) === v && codes.has(c)))) return false
  }
  return true
}
