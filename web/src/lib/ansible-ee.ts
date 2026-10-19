import { apiFetch } from './api'

/**
 * Which Ansible execution environments this deployment can run (#2154).
 *
 * `listing` is the reason this does not go through `fetchAllPages`, which
 * discards `meta`. The built-in set is read from a container registry, so it
 * can be unavailable — and an empty list means opposite things in the two
 * states:
 *
 *   complete → the registry answered. An absent line does not exist, and the
 *              server will refuse a workspace naming it. Offer a picker.
 *   unknown  → the set could not be determined. The server accepts ANY line,
 *              so the form must let someone type one. Offering an empty picker
 *              here would block work the server would have allowed.
 *
 * Operator-supplied labels are authoritative in both states: they come from the
 * deployment's own configuration rather than from a registry.
 */
export type EEListing = 'complete' | 'unknown'

export interface AnsibleExecutionEnvironments {
  /** Built-in ansible-core lines, newest first. */
  lines: string[]
  /** Operator-supplied labels. */
  labels: string[]
  listing: EEListing
}

/** `unknown` with nothing in it — the state that makes every field free-text. */
export const EE_UNKNOWN: AnsibleExecutionEnvironments = {
  lines: [],
  labels: [],
  listing: 'unknown',
}

interface EEResource {
  id?: string
  attributes?: {
    kind?: string
    'ansible-core-line'?: string | null
    label?: string | null
  }
}

/**
 * Non-fatal by design, like the other pickers on these forms: any failure
 * degrades to `EE_UNKNOWN`, which renders free-text inputs rather than an empty
 * picker. A page must not become unusable because one listing could not be read.
 */
export async function fetchAnsibleExecutionEnvironments(): Promise<AnsibleExecutionEnvironments> {
  try {
    const res = await apiFetch('/api/v1/ansible/execution-environments')
    if (!res.ok) return EE_UNKNOWN
    const body = (await res.json()) as { data?: EEResource[]; meta?: { listing?: string } }
    const lines: string[] = []
    const labels: string[] = []
    for (const d of body.data ?? []) {
      const a = d.attributes ?? {}
      if (a.kind === 'operator' && a.label) labels.push(a.label)
      else if (a.kind === 'built-in' && a['ansible-core-line']) lines.push(a['ansible-core-line'])
    }
    // An absent `meta.listing` reads as unknown, never complete: a server
    // predating the field cannot tell us, and assuming it is authoritative
    // would present an empty set as "nothing available".
    const listing: EEListing = body.meta?.listing === 'complete' ? 'complete' : 'unknown'
    return { lines, labels, listing }
  } catch {
    return EE_UNKNOWN
  }
}
