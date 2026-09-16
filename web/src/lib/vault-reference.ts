/**
 * Parse and rebuild a Vault variable reference (#1439, #1619, #1648).
 *
 * A Vault-sourced variable's value is a JSON *reference*, not a secret:
 *
 *   {"source":"vault","mount":"secret","path":"apps/x","field":"token",
 *    "engine":"dynamic","method":"POST","data":{…},"file":{"name":"gcp/adc.json"}}
 *
 * A file's content is exactly one of (#1648): the reference's `field`
 * (optionally with `file.encoding: "base64"`), a `file.template` over the whole
 * secret, or a `file.format` (`json`/`env`, optional `file.fields`). The last
 * two carry no `field`.
 *
 * The UI renders only some of those keys as form fields. Everything else —
 * `method`, `data`, keys inside `file` the form does not render, and whatever a
 * later release adds — must survive an edit untouched. Before #1619 each page
 * rebuilt the reference from the on-screen fields alone, so saving any edit
 * silently dropped `method` and `data`, and would have dropped `file` too.
 *
 * The rule this module keeps: **a key the user did not edit is written back
 * exactly as it was stored, in the same position.** Only edited keys change,
 * and an edit that changes nothing returns the stored string verbatim.
 *
 * Pure TypeScript with no framework imports, so the `node --test` unit suite
 * can load it directly.
 */

/** Where a file's content comes from (#1648). */
export type VaultFileContent = 'field' | 'template' | 'format'

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
  /** What the file holds: one field, a template, or the whole secret (#1648). */
  fileContent: VaultFileContent
  /** `file.template`, verbatim (whitespace is significant). */
  template: string
  /** `file.format` — `json` or `env`; empty when the reference has none. */
  format: string
  /** `file.fields` as the form edits it: names separated by commas. */
  fields: string
  /** `file.encoding` — `base64`, or empty for the field as stored. */
  encoding: string
  /** The stored reference as parsed, so unedited keys can be written back. */
  original: Record<string, unknown>
  /** The stored string, returned verbatim when nothing was edited. */
  raw: string
}

type FormFields = Omit<VaultReferenceValue, 'original' | 'raw'>

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

const str = (v: unknown) => (typeof v === 'string' ? v : '')

/** Split the form's fields box into names: commas or newlines, trimmed, no blanks. */
export function parseFieldList(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** The form fields a stored reference implies, untrimmed and unnormalised. */
function fieldsOf(ref: Record<string, unknown>): FormFields {
  const file = isPlainObject(ref.file) ? ref.file : null
  const has = (k: string) => file !== null && file[k] !== undefined && file[k] !== null
  const fileContent: VaultFileContent = has('template')
    ? 'template'
    : has('format')
      ? 'format'
      : 'field'
  return {
    instance: str(ref.vault),
    mount: str(ref.mount),
    path: str(ref.path),
    field: str(ref.field),
    engine: ref.engine === 'dynamic' ? 'dynamic' : 'kv2',
    file: file !== null,
    fileName: file ? str(file.name) : '',
    fileContent,
    template: file ? str(file.template) : '',
    format: file ? str(file.format) : '',
    fields:
      file && Array.isArray(file.fields)
        ? file.fields.filter((f): f is string => typeof f === 'string').join(', ')
        : '',
    encoding: file ? str(file.encoding) : '',
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

/** Whether the form should show (and require) the reference's `field`. */
export function usesField(v: Pick<VaultReferenceValue, 'file' | 'fileContent'>): boolean {
  return !v.file || v.fileContent === 'field'
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
  for (const k of ['mount', 'path'] as const) {
    if (edited(k) || (isNew && !(k in ref))) ref[k] = v[k].trim()
  }
  // `field` belongs to the reference only while the file (if any) holds one
  // field. A template or a format builds the file from the whole secret, and
  // the server refuses `field` beside them.
  const wantsField = usesField(v)
  if (wantsField) {
    // Coming back from a template or format, write the field only once one is
    // typed: until then the server's "missing: field" is the right message,
    // and an empty key would be a change the user did not make.
    const becameField = !usesField(base) && v.field.trim() !== ''
    if (edited('field') || becameField || (isNew && !('field' in ref))) ref.field = v.field.trim()
  } else if (edited('file') || edited('fileContent') || isNew) {
    delete ref.field
  }
  if (edited('engine')) {
    if (v.engine === 'kv2') delete ref.engine
    else ref.engine = v.engine
  }

  const fileKeys: (keyof FormFields)[] = [
    'file',
    'fileName',
    'fileContent',
    'template',
    'format',
    'fields',
    'encoding',
  ]
  if (fileKeys.some(edited)) {
    if (!v.file) {
      delete ref.file
    } else {
      const file: Record<string, unknown> = isPlainObject(v.original.file)
        ? { ...v.original.file }
        : {}
      if (edited('file') || edited('fileName')) {
        // Omitted rather than sent empty, so the server applies its default
        // (the variable key) instead of rejecting an empty name.
        if (v.fileName.trim()) file.name = v.fileName.trim()
        else delete file.name
      }
      if (edited('fileContent')) {
        // Switching what the file holds clears the other kinds' keys: the
        // server refuses a template beside a format, or an encoding beside
        // either, and a stale one would be a surprise.
        for (const k of ['template', 'format', 'fields', 'encoding']) delete file[k]
        if (v.fileContent === 'template') file.template = v.template
        if (v.fileContent === 'format') {
          file.format = v.format || 'json'
          const list = parseFieldList(v.fields)
          if (list.length) file.fields = list
        }
        if (v.fileContent === 'field' && v.encoding) file.encoding = v.encoding
      } else {
        if (v.fileContent === 'template' && edited('template')) file.template = v.template
        if (v.fileContent === 'format') {
          if (edited('format')) file.format = v.format || 'json'
          if (edited('fields')) {
            const list = parseFieldList(v.fields)
            if (list.length) file.fields = list
            else delete file.fields
          }
        }
        if (v.fileContent === 'field' && edited('encoding')) {
          if (v.encoding) file.encoding = v.encoding
          else delete file.encoding
        }
      }
      ref.file = file
    }
  }
  return JSON.stringify(ref)
}
