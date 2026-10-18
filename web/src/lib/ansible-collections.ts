/**
 * Ansible collection entries, between the form and the wire (#1931).
 *
 * The API stores and returns `{ name, version? }`, which is ansible's own
 * `requirements.yml` shape. A form is far easier to use as one line per
 * collection, so these two functions are the only place the two shapes meet —
 * written once because a second copy is how the bulk form and the
 * autodiscovery form come to disagree about what they are sending.
 *
 * The line form is `namespace.name` or `namespace.name:version`, which is
 * `ansible-galaxy install`'s own argument form, so it is already familiar.
 * Nothing here validates: the server refuses a malformed entry by name, and a
 * second rule in the browser would be a second rule to keep in step.
 */

export type AnsibleCollection = { name: string; version?: string }

/** `["ns.coll:1.2.3"]` → `[{ name: "ns.coll", version: "1.2.3" }]` */
export function parseCollectionLines(lines: string[]): AnsibleCollection[] {
  const out: AnsibleCollection[] = []
  for (const raw of lines) {
    const line = raw.trim()
    if (!line) continue
    const at = line.indexOf(':')
    if (at === -1) {
      out.push({ name: line })
    } else {
      const version = line.slice(at + 1).trim()
      const entry: AnsibleCollection = { name: line.slice(0, at).trim() }
      if (version) entry.version = version
      out.push(entry)
    }
  }
  return out
}

/** `[{ name: "ns.coll", version: "1.2.3" }]` → `["ns.coll:1.2.3"]` */
export function formatCollectionLines(entries: AnsibleCollection[] | undefined): string[] {
  return (entries || []).map((e) => (e.version ? `${e.name}:${e.version}` : e.name))
}
