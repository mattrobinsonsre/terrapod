/* ── The resolved inventory is hierarchical, so it is shown as a hierarchy ──
   (#2413 F8)

   What this replaces: one display used three times -- a name, then everything
   else in a single monospace run. `formatVars` space-joined `k=v` pairs, which
   is AMBIGUOUS the moment a value contains a space, and `ansible_ssh_common_args`
   routinely does (`-o ProxyCommand="ssh -W %h:%p bastion"`). Nesting showed one
   hop while the view's own hint promised transitive reach, and merged variables
   carried no provenance at all -- which is the whole reason a resolved view
   exists rather than a list of the declared rows.

   Two displays, each matched to its content: a TREE for the structure, because
   the structure is a tree; and YAML for one node's variables, because that is
   the format the thing being described is written in.

   Nothing collapses. The tree renders fully expanded as nested `<ul>`/`<li>` --
   structure made visible rather than hidden behind a disclosure, which is the
   same decision as the forms (#2413 F4). */

export interface VarProvenance {
  value: unknown
  origin: string
  chain: { level: string; value: string }[]
}

/** One variable as a YAML line, with its origin as a trailing comment. */
export function varYamlLine(key: string, prov: VarProvenance | undefined, raw: unknown): string {
  const value = prov ? prov.value : raw
  // JSON is valid YAML, so a string with spaces, a colon or a `#` is quoted
  // correctly by `JSON.stringify` and cannot be misread -- which is exactly
  // what the space-joined `k=v` run could not promise.
  return `${key}: ${JSON.stringify(value)}`
}

/** A node of the structure tree: a group with children and hosts, or a host. */
export interface TreeNode {
  name: string
  kind: 'group' | 'host'
  children: TreeNode[]
}

/**
 * The structure as a tree, rooted at the groups nothing nests under.
 *
 * `all` and `ungrouped` are ansible's implicit groups and are deliberately
 * absent from `group-children`, so the roots are derived: every group that is
 * no other group's child. Hosts belonging to no group at all are collected
 * under their own root, because a host the view does not show is a host nobody
 * knows is there.
 *
 * Cycles cannot occur -- the API refuses them on the nesting write -- but the
 * walk carries a `seen` set anyway: this renders whatever the server sent, and
 * a render that hangs is worse than one that stops.
 */
export function buildTree(
  groups: Record<string, string[]>,
  nesting: Record<string, string[]>,
  allHosts: string[],
): { roots: TreeNode[]; ungrouped: string[] } {
  const groupNames = new Set([...Object.keys(groups), ...Object.keys(nesting)])
  for (const kids of Object.values(nesting)) for (const k of kids) groupNames.add(k)

  const isChild = new Set<string>()
  for (const kids of Object.values(nesting)) for (const k of kids) isChild.add(k)

  const walk = (name: string, seen: Set<string>): TreeNode => {
    const next = new Set(seen).add(name)
    const children: TreeNode[] = []
    for (const kid of (nesting[name] ?? []).slice().sort()) {
      if (!next.has(kid)) children.push(walk(kid, next))
    }
    for (const host of (groups[name] ?? []).slice().sort()) {
      children.push({ name: host, kind: 'host', children: [] })
    }
    return { name, kind: 'group', children }
  }

  const roots = [...groupNames]
    .filter((g) => !isChild.has(g))
    .sort()
    .map((g) => walk(g, new Set()))

  // A host in no group at all. Ansible calls these `ungrouped`, and that group
  // is implicit so it never reaches `groups` -- so a view that only walked the
  // group tree would silently omit them, and a host nobody knows is there is
  // the worst thing an inventory view can hide.
  const placed = new Set<string>()
  for (const members of Object.values(groups)) for (const h of members) placed.add(h)
  const ungrouped = allHosts.filter((h) => !placed.has(h)).sort()

  return { roots, ungrouped }
}

