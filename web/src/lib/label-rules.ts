/**
 * Parsing and rendering for a role's allow/deny label rule.
 *
 * A rule binds each key to the values that satisfy it, so `{env: ['prod',
 * 'stg']}` reads as "env is prod OR stg". The server has always stored and
 * enforced that shape; until 2.0 no client could carry it, and the role form
 * refused a repeated key outright rather than let the second value silently
 * overwrite the first (#1457).
 *
 * Lives here rather than inside the roles page so it can be tested directly —
 * a rule that quietly drops a clause is a permissions bug, and the shape that
 * used to break things was precisely the one nothing exercised.
 */

/** A label rule: each key maps to the values that satisfy it (matched as OR). */
export type LabelRule = Record<string, string[]>

/**
 * Parse the comma-separated `key=value` form the role editor uses.
 *
 * A key repeated with DIFFERENT values accumulates — `env=prod, env=stg`
 * becomes `{env: ['prod', 'stg']}`. Repeated with the SAME value it stays one
 * value: that is a typo, not a request for a duplicate clause.
 *
 * A bare `key` with no `=` matches the empty-string value, which is how the
 * editor has always represented "key present, value irrelevant".
 */
export function parseLabelRule(input: string): LabelRule {
  const result: LabelRule = {}
  if (!input.trim()) return result

  for (const pair of input.split(',')) {
    const [rawKey, rawValue] = pair.split('=').map((x) => x.trim())
    if (!rawKey) continue
    const value = rawValue || ''
    const existing = result[rawKey]
    if (!existing) result[rawKey] = [value]
    else if (!existing.includes(value)) existing.push(value)
  }
  return result
}

/**
 * Render a rule back into the editor's text form — the inverse of
 * {@link parseLabelRule}, so a key with several values becomes several
 * `key=value` pairs, which is what the operator typed to create it.
 *
 * Tolerates a bare string per key as well as a list: the server may send
 * either, and a role fetched before a round-trip through the SDK can still
 * carry the scalar form.
 */
export function formatLabelRule(labels: Record<string, string[] | string>): string {
  return Object.entries(labels)
    .flatMap(([key, value]) => {
      const values = Array.isArray(value) ? value : [value]
      return values.map((one) => (one ? `${key}=${one}` : key))
    })
    .join(', ')
}
