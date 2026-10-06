/**
 * The per-provider-configuration audience map and the two pure helpers that
 * decide what goes on the wire (#1901).
 *
 * Kept in a `.ts` module rather than beside the editor in
 * `components/template-editors.tsx` so `npm run test:unit` can import it:
 * node's type-stripping handles `.ts` but not the JSX in a `.tsx`. The editor
 * re-exports both, so a page needs only the one import.
 */

/**
 * A key is the provider as a configuration names it — `aws`, or `aws.west`
 * for one aliased configuration — and the alias IS part of the key: it is
 * never split on the dot and never nested, because `aws.west` and `aws` are
 * two independent lookups on the server (specific, then general).
 *
 * The value is always a LIST, even with one entry; several entries mean
 * "these audiences are interchangeable for this target".
 *
 * **An audience is opaque.** The federation target chose the string and the
 * server stores it byte-for-byte, so nothing here trims, lower-cases or
 * otherwise canonicalises a value — `sanitizeOidcAudiences` only drops rows
 * that are entirely blank. Normalising would also make the Terraform
 * provider's plan disagree with its own apply.
 */
export type OidcAudiences = Record<string, string[]>

/** A provider name has no whitespace and at most one dot, and cannot start or
 *  end with one — the same three cheap checks the server applies. Enforced
 *  here only to keep a typo from 422-ing a whole fleet update; the server is
 *  the authority and deliberately does not impose a grammar beyond this, so
 *  an unfamiliar provider name is accepted. */
export function isValidOidcProvider(key: string): boolean {
  const k = key.trim()
  if (!k) return false
  if (/\s/.test(k)) return false
  if ((k.match(/\./g) || []).length > 1) return false
  if (k.startsWith('.') || k.endsWith('.')) return false
  return true
}

/**
 * What to put on the wire: blank audience rows dropped, and any provider left
 * with no audiences dropped with them.
 *
 * Both halves matter. The server REFUSES a blank entry rather than ignoring
 * it, so an empty row left behind in the editor would 422 the save; and it
 * refuses an explicitly empty list too, because removing the key is the
 * defined way to stop overriding a provider (it then falls back to the
 * deployment's own audiences) while `[]` is neither an override nor a removal.
 *
 * Provider keys ARE trimmed — the server rejects any whitespace in a key
 * outright, so trimming can only avoid a pointless rejection. Audience values
 * are not, per the type doc above.
 */
export function sanitizeOidcAudiences(value: OidcAudiences): OidcAudiences {
  const out: OidcAudiences = {}
  for (const [key, list] of Object.entries(value)) {
    const k = key.trim()
    if (!k) continue
    const kept = (list || []).filter((v) => v.trim() !== '')
    if (kept.length === 0) continue
    out[k] = kept
  }
  return out
}
