// Pure helpers for the workspace-list filter input.
//
// The input is a single text box that mixes name substrings, label predicates,
// and status predicates separated by whitespace. Each token is either:
//
//   - bare word          → name substring match (case-insensitive)
//   - "status:value"     → status predicate (resolved-status exact match)
//   - "key:value"        → label predicate (case-sensitive exact match)
//   - "key=value"        → same as key:value
//   - "key:" / "key="    → label key-only predicate (any value, including empty)
//
// All terms are AND-ed: a workspace matches when every name term appears in
// its name AND every label predicate matches one of its labels AND every
// status predicate matches the workspace's resolved status.
//
// `status` is treated specially because it isn't a label — it's a derived
// virtual field computed from latest-run / drift-status / state-diverged /
// vcs-last-error. The accepted values are kebab-case lowercase versions of
// the labels in `resolveStatus` on the workspaces page (see STATUS_FILTER
// constants below).
//
// `status` (and the other reserved keys — see `RESERVED_LABEL_KEYS` in
// `services/terrapod/services/label_validation.py`) cannot be used as
// literal label keys: the API rejects them at create/update time so the
// filter language stays unambiguous. The user-facing list lives in
// `docs/rbac.md` § Reserved label keys.

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

export type FilterTerm = NameTerm | LabelTerm | StatusTerm

export interface ParsedFilter {
  terms: FilterTerm[]
}

// Filter values for the two preset buttons. Kept as exported constants so
// the buttons + matcher + tests stay in lockstep on the exact string used.
export const STATUS_FILTER_ERRORED = 'errored'
export const STATUS_FILTER_NEEDS_CONFIRM = 'needs-confirm'

const SEPARATOR = /[:=]/

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
    if (key === 'status') {
      // Status is virtual-only — `status:` with no value would be ambiguous
      // (no workspace has a "label key" called status), so drop empty.
      if (rest === '') continue
      terms.push({ kind: 'status', value: rest })
      continue
    }
    terms.push({ kind: 'label', key, value: rest === '' ? null : rest })
  }
  return { terms }
}

export function serializeFilter(parsed: ParsedFilter): string {
  return parsed.terms
    .map(t => {
      if (t.kind === 'name') return t.value
      if (t.kind === 'status') return `status:${t.value}`
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

export interface MatchableWorkspace {
  attributes: {
    name: string
    labels?: Record<string, string> | null
  }
}

/**
 * @param resolvedStatus  Caller-resolved status string for this workspace
 *   (e.g. "errored", "needs-confirm"). Only consulted when the filter
 *   contains status terms; safe to omit otherwise. The page resolves it
 *   from `latest-run` + drift / divergence / VCS-error signals via the
 *   `resolveStatus` helper, so this module stays UI-agnostic.
 */
export function matchWorkspace(
  ws: MatchableWorkspace,
  parsed: ParsedFilter,
  resolvedStatus?: string,
): boolean {
  const name = ws.attributes.name.toLowerCase()
  const labels = ws.attributes.labels || {}
  for (const term of parsed.terms) {
    if (term.kind === 'name') {
      if (!name.includes(term.value.toLowerCase())) return false
    } else if (term.kind === 'status') {
      if (resolvedStatus !== term.value) return false
    } else if (term.value === null) {
      if (!Object.prototype.hasOwnProperty.call(labels, term.key)) return false
    } else if (labels[term.key] !== term.value) {
      return false
    }
  }
  return true
}
