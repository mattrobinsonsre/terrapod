/**
 * Parse and rebuild a Vault variable reference (#1439, #1619).
 *
 * A Vault-sourced variable's value is a JSON *reference*, not a secret:
 *
 *   {"source":"vault","mount":"secret","path":"apps/x","field":"token",
 *    "engine":"dynamic","method":"POST","data":{…},"file":{"name":"gcp/adc.json"}}
 *
 * The UI renders only some of those keys as form fields. Everything else —
 * `method`, `data`, keys inside `file` beyond `name`, and whatever a later
 * release adds — must survive an edit untouched. Before #1619 each page rebuilt
 * the reference from the on-screen fields alone, so saving any edit silently
 * dropped `method` and `data`, and would have dropped `file` too.
 *
 * The rule this module keeps: **a key the user did not edit is written back
 * exactly as it was stored, in the same position.** Only edited keys change,
 * and an edit that changes nothing returns the stored string verbatim.
 *
 * Pure TypeScript with no framework imports, so the `node --test` unit suite
 * can load it directly.
 */

export interface VaultReferenceValue {
  instance: string
  mount: string
  path: string
  field: string
  engine: 'kv2' | 'dynamic'
  /** Deliver the secret as a file on the runner (#1619). */
  file: boolean
  /** The file name. Empty means the server's default, the variable key. */
  fileName: string
  /** The stored reference as parsed, so unedited keys can be written back. */
  original: Record<string, unknown>
  /** The stored string, returned verbatim when nothing was edited. */
  raw: string
}

type FormFields = Pick<
  VaultReferenceValue,
  'instance' | 'mount' | 'path' | 'field' | 'engine' | 'file' | 'fileName'
>

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

const str = (v: unknown) => (typeof v === 'string' ? v : '')

/** The form fields a stored reference implies, untrimmed and unnormalised. */
function fieldsOf(ref: Record<string, unknown>): FormFields {
  const file = isPlainObject(ref.file)
  return {
    instance: str(ref.vault),
    mount: str(ref.mount),
    path: str(ref.path),
    field: str(ref.field),
    engine: ref.engine === 'dynamic' ? 'dynamic' : 'kv2',
    file,
    fileName: file ? str((ref.file as Record<string, unknown>).name) : '',
  }
}

export function emptyVaultReference(): VaultReferenceValue {
  return { ...fieldsOf({}), original: {}, raw: '' }
}

/** Load a stored reference into the form. */
export function parseVaultReference(value: string): VaultReferenceValue {
  let parsed: unknown
  try {
    parsed = JSON.parse(value || '{}')
  } catch {
    // Unreadable: start from nothing rather than echo garbage back on save.
    return emptyVaultReference()
  }
  if (!isPlainObject(parsed)) return emptyVaultReference()
  return { ...fieldsOf(parsed), original: parsed, raw: value }
}

/** Serialise the form, changing only the keys the user edited. */
export function buildVaultReference(v: VaultReferenceValue): string {
  const base = fieldsOf(v.original)
  const edited = (k: keyof FormFields) => v[k] !== base[k]
  const anyEdit = (Object.keys(base) as (keyof FormFields)[]).some(edited)
  if (!anyEdit && v.raw) return v.raw

  // Start from the stored object so every key keeps its value and position.
  // A reference with no `source` (only ever a brand-new one) gets it first.
  const ref: Record<string, unknown> =
    'source' in v.original ? { ...v.original } : { source: 'vault', ...v.original }

  if (edited('instance')) {
    if (v.instance.trim()) ref.vault = v.instance.trim()
    else delete ref.vault
  }
  // A brand-new reference always carries the coordinates, even empty, so the
  // server's message about a missing one is what the operator sees. A stored
  // one is left alone unless edited: a reference that deliberately omits a
  // coordinate (a whole-secret file has no `field`) must not gain one.
  const isNew = Object.keys(v.original).length === 0
  for (const k of ['mount', 'path', 'field'] as const) {
    if (edited(k) || (isNew && !(k in ref))) ref[k] = v[k].trim()
  }
  if (edited('engine')) {
    if (v.engine === 'kv2') delete ref.engine
    else ref.engine = v.engine
  }
  if (edited('file') || edited('fileName')) {
    if (!v.file) {
      delete ref.file
    } else {
      const file: Record<string, unknown> = isPlainObject(v.original.file)
        ? { ...v.original.file }
        : {}
      // Omitted rather than sent empty, so the server applies its default
      // (the variable key) instead of rejecting an empty name.
      if (v.fileName.trim()) file.name = v.fileName.trim()
      else delete file.name
      ref.file = file
    }
  }
  return JSON.stringify(ref)
}
