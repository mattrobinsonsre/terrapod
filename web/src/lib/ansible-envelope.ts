/**
 * The Ansible credential envelope, as the form reads and writes it (#1933).
 *
 * A module rather than two functions inside the component, because both are
 * pure and both carry a decision worth pinning: which stored fields the form
 * will still show, and what an empty field serialises to. `node:test` strips
 * types from `.ts` and cannot load a `.tsx`, so a tested helper has to live
 * here — the same reason `runHref` sits in `run-phases.ts`.
 *
 * The field NAMES are the server's (`services/terrapod/services/ansible_auth.py`)
 * and are not restated as a list here: the form renders the inputs it has, and
 * the server refuses anything it does not carry. The one exception is the
 * retired set below, which exists precisely because those names are no longer
 * in the server's set.
 */

export type AnsibleEnvelope = Record<string, string>

/**
 * Fields the machine credential USED to carry, dropped on parse (#2425 §0.3).
 *
 * This is what makes the form the REPAIR path for a credential written before
 * the set narrowed. Without it the parse kept every string key, the save wrote
 * `{...fields, [field]: next}` back including the retired one, and the server
 * refused it — so an operator could not fix, through the UI, a row the UI no
 * longer renders an input for. Dropping them here means editing any field at
 * all rewrites the envelope in its current shape.
 *
 * The server tolerates them on read for the same reason, so nothing is lost
 * between the two: the credential keeps working until someone edits it, and
 * editing it cleans it up. Their home is the inventory
 * (`ansible_user`, `ansible_become`, …), where they are visible in a diff and
 * settable per host.
 */
export const RETIRED_MACHINE_FIELDS = new Set(['user', 'become', 'become_method', 'become_user'])

/** The stored value as an envelope; `{}` for anything unreadable. */
export function parseAnsibleEnvelope(value: string): AnsibleEnvelope {
  if (!value.trim()) return {}
  try {
    const parsed: unknown = JSON.parse(value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const out: AnsibleEnvelope = {}
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === 'string' && !RETIRED_MACHINE_FIELDS.has(k)) out[k] = v
    }
    return out
  } catch {
    return {}
  }
}

/**
 * The envelope as the API stores it, or `''` when every field is empty.
 *
 * Empty fields are OMITTED rather than sent as empty strings: the server reads
 * `private_key: ""` as a supplied-but-empty key and refuses the credential for
 * having neither secret, which names the wrong problem. And an envelope with
 * nothing in it serialises to `''`, not `'{}'`, because the edit path sends
 * `value` only when it is non-empty — so clearing every field leaves the
 * stored credential alone instead of replacing it with an empty object the
 * server would refuse.
 */
export function serializeAnsibleEnvelope(fields: AnsibleEnvelope): string {
  const out: AnsibleEnvelope = {}
  for (const [k, v] of Object.entries(fields)) {
    if (v !== '') out[k] = v
  }
  return Object.keys(out).length ? JSON.stringify(out) : ''
}
