// Pure helpers for the workspace-list filter input.
//
// The input is a single text box that mixes name substrings and label
// predicates separated by whitespace. Each token is either:
//
//   - bare word          → name substring match (case-insensitive)
//   - "key:value"        → label predicate (case-sensitive exact match)
//   - "key=value"        → same as key:value
//   - "key:" / "key="    → label key-only predicate (any value, including empty)
//
// All terms are AND-ed: a workspace matches when every name term appears in
// its name AND every label predicate matches one of its labels.

export interface NameTerm {
  kind: 'name'
  value: string
}

export interface LabelTerm {
  kind: 'label'
  key: string
  value: string | null // null = any-value match
}

export type FilterTerm = NameTerm | LabelTerm

export interface ParsedFilter {
  terms: FilterTerm[]
}

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
    terms.push({ kind: 'label', key, value: rest === '' ? null : rest })
  }
  return { terms }
}

export function serializeFilter(parsed: ParsedFilter): string {
  return parsed.terms
    .map(t => {
      if (t.kind === 'name') return t.value
      return t.value === null ? `${t.key}:` : `${t.key}:${t.value}`
    })
    .join(' ')
}

export function removeTerm(parsed: ParsedFilter, index: number): ParsedFilter {
  return { terms: parsed.terms.filter((_, i) => i !== index) }
}

export interface MatchableWorkspace {
  attributes: {
    name: string
    labels?: Record<string, string> | null
  }
}

export function matchWorkspace(ws: MatchableWorkspace, parsed: ParsedFilter): boolean {
  const name = ws.attributes.name.toLowerCase()
  const labels = ws.attributes.labels || {}
  for (const term of parsed.terms) {
    if (term.kind === 'name') {
      if (!name.includes(term.value.toLowerCase())) return false
    } else if (term.value === null) {
      if (!Object.prototype.hasOwnProperty.call(labels, term.key)) return false
    } else if (labels[term.key] !== term.value) {
      return false
    }
  }
  return true
}
