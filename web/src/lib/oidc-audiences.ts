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

/** What `GET /api/terrapod/v1/oidc/audience-defaults` carries, flattened. */
export interface OidcAudienceDefaults {
  /** The deployment-wide catalogue a workspace's own map merges over. Empty
   *  when the deployment configures none, which is the default. */
  audiences: OidcAudiences
  /** False when the deployment publishes no OpenID Connect issuer at all, so
   *  nothing configured here can take effect. Distinct from an empty
   *  catalogue: the issuer can be on with nothing configured. */
  issuerEnabled: boolean
}

/** Order-sensitive list equality. Order matters and is not an implementation
 *  detail: the entries are what goes into `aud`, and the server treats a
 *  reorder as a change for exactly that reason, so treating one as equal here
 *  would hide a real difference. */
export function sameAudiences(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false
  return a.every((v, i) => v === b[i])
}

export interface OidcAudiencePartition {
  /**
   * The entries this workspace OWNS — present in the effective map and either
   * absent from the catalogue or differing from it. This is the set that goes
   * back on the wire, and sending only this is what stops a save promoting
   * every inherited entry into an override.
   */
  owned: OidcAudiences
  /**
   * The catalogue value for every key the effective map contains — including
   * the keys currently owned, which is deliberate: it is what each key would
   * FALL BACK to, so removing an owned entry can reveal the default in place
   * rather than making the key vanish. The editor renders the ones not present
   * in `owned`, so the two cannot show the same key twice.
   *
   * A catalogue entry the server dropped as malformed is absent from the
   * effective map and so absent here too, which is right — it is not in force,
   * and showing it as inherited would claim otherwise.
   */
  fallbacks: OidcAudiences
}

/**
 * Split the merged map a workspace read returns into what the workspace owns
 * and what it inherits (#1901).
 *
 * The workspace endpoint returns the catalogue with the workspace's own map
 * merged over it per key, with no marker saying which is which — so the only
 * way to tell them apart is to fetch the catalogue and subtract. Without this
 * the UI cannot show provenance, and worse, writing the merged value back
 * wholesale would turn every inherited entry into an override.
 *
 * **One accepted fidelity loss, deliberate — do not try to engineer round it.**
 * An override whose value happens to EQUAL the default is indistinguishable
 * from an inherited entry, so it reads as inherited and is dropped from the
 * next save. The effective audiences do not change (the key falls back to the
 * identical default), so the outcome is benign. This is the same loss AWS's
 * `tags`/`default_tags` has lived with for years, and it is accepted on the
 * provider side too.
 */
export function partitionOidcAudiences(
  merged: OidcAudiences,
  defaults: OidcAudiences,
): OidcAudiencePartition {
  const owned: OidcAudiences = {}
  const fallbacks: OidcAudiences = {}
  for (const [key, list] of Object.entries(merged || {})) {
    const fallback = (defaults || {})[key]
    if (fallback) fallbacks[key] = [...fallback]
    if (!fallback || !sameAudiences(list || [], fallback)) owned[key] = [...(list || [])]
  }
  return { owned, fallbacks }
}
