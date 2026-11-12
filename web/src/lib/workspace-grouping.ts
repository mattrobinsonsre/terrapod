export interface WorkspaceGroup<T = unknown> {
  key: string
  label: string
  workspaces: WorkspaceItem<T>[]
  children: WorkspaceGroup<T>[]
}

export interface WorkspaceItem<T = unknown> {
  id: string
  name: string
  workspace: T
}

export type GroupMode = 'flat' | 'repo' | 'repo-path'

export const LOCAL_GROUP_KEY = '__local__'

export function parseGroupParam(param: string | null): GroupMode {
  if (param === 'repo') return 'repo'
  if (param === 'repo-path') return 'repo-path'
  return 'flat'
}

export function serializeGroupParam(mode: GroupMode): string | null {
  if (mode === 'flat') return null
  return mode
}

type WsConstraint = { id: string; attributes: { name: string; 'working-directory'?: string; 'vcs-repo-url'?: string } }

export function buildWorkspaceTree<T extends WsConstraint>(
  workspaces: T[],
  mode: GroupMode,
): WorkspaceGroup<T>[] {
  if (mode === 'flat') return []
  const { repoGroups, localWorkspaces } = partitionByRepo(workspaces)
  const result: WorkspaceGroup<T>[] = []

  for (const { key, label, workspaces: repoWs } of repoGroups) {
    const group: WorkspaceGroup<T> = { key, label, workspaces: [], children: [] }
    for (const ws of repoWs) {
      insertWorkspace(group, ws, mode === 'repo-path')
    }
    sortGroups([group])
    result.push(group)
  }

  if (localWorkspaces.length > 0) {
    const local: WorkspaceGroup<T> = { key: LOCAL_GROUP_KEY, label: LOCAL_GROUP_KEY, workspaces: [], children: [] }
    for (const ws of localWorkspaces) {
      insertWorkspace(local, ws, mode === 'repo-path')
    }
    sortGroups([local])
    result.push(local)
  }

  return result
}

function normalizeRepoUrl(url: string): string {
  let cleaned = url.replace(/\.git$/, '').replace(/\/+$/, '').toLowerCase()
  const sshMatch = cleaned.match(/^[^@]+@([^:]+):(.+)$/)
  if (sshMatch) cleaned = `${sshMatch[1]}/${sshMatch[2]}`
  else cleaned = cleaned.replace(/^https?:\/\//, '')
  return cleaned
}

function keySegments(key: string): string[] {
  return key.split('/').filter(Boolean)
}

function lastSegments(key: string, n: number): string {
  const segments = keySegments(key)
  return (n <= 0 ? segments : segments.slice(-n)).join('/')
}

// Label a repo group with the shortest suffix of its normalised key
// (host/owner/name) that is unique across the whole result set. A lone repo
// stays a bare basename; two repos sharing a basename fall back to owner/name;
// the same owner/name on different hosts falls back to the full host/owner/name.
// Working off the normalised key (not the raw first-seen URL) keeps the label
// deterministic — case-, trailing-slash-, and .git-insensitive.
function labelForKeys(keys: string[]): Map<string, string> {
  const maxDepth = Math.max(1, ...keys.map(k => keySegments(k).length))
  const labels = new Map<string, string>()
  const pending = new Set(keys)

  for (let depth = 1; depth <= maxDepth && pending.size > 0; depth++) {
    const counts = new Map<string, number>()
    for (const key of pending) {
      const suffix = lastSegments(key, depth)
      counts.set(suffix, (counts.get(suffix) ?? 0) + 1)
    }
    for (const key of [...pending]) {
      const suffix = lastSegments(key, depth)
      if (counts.get(suffix) === 1) {
        labels.set(key, suffix)
        pending.delete(key)
      }
    }
  }
  // Anything still colliding at full depth (identical keys shouldn't happen —
  // they'd be the same group) gets the full key as a last resort.
  for (const key of pending) labels.set(key, key)
  return labels
}

function partitionByRepo<T extends WsConstraint>(workspaces: T[]) {
  const vcs = workspaces.filter(ws => ws.attributes['vcs-repo-url'])
  const local = workspaces.filter(ws => !ws.attributes['vcs-repo-url'])

  const byRepo = new Map<string, T[]>()
  for (const ws of vcs) {
    const key = normalizeRepoUrl(ws.attributes['vcs-repo-url']!)
    if (!byRepo.has(key)) byRepo.set(key, [])
    byRepo.get(key)!.push(ws)
  }

  const labels = labelForKeys([...byRepo.keys()])
  const repoGroups = Array.from(byRepo.entries())
    .map(([key, ws]) => ({ key, label: labels.get(key) ?? key, workspaces: ws }))
    .sort((a, b) => a.label.localeCompare(b.label))

  return { repoGroups, localWorkspaces: local }
}

function insertWorkspace<T extends WsConstraint>(
  group: WorkspaceGroup<T>,
  ws: T,
  nestByPath: boolean,
) {
  const dir = (ws.attributes['working-directory'] || '').trim().replace(/^\/+/, '')
  if (!nestByPath || !dir) {
    group.workspaces.push({ id: ws.id, name: ws.attributes.name, workspace: ws })
    return
  }

  const segments = dir.split('/').filter(Boolean)
  let current = group.children

  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i]
    let child = current.find(g => g.key === seg)
    if (!child) {
      child = { key: seg, label: seg, workspaces: [], children: [] }
      current.push(child)
    }
    if (i === segments.length - 1) {
      child.workspaces.push({ id: ws.id, name: ws.attributes.name, workspace: ws })
    } else {
      current = child.children
    }
  }
}

function sortGroups<T>(groups: WorkspaceGroup<T>[]): WorkspaceGroup<T>[] {
  groups.sort((a, b) => a.label.localeCompare(b.label))
  for (const g of groups) {
    g.children = sortGroups(g.children)
  }
  return groups
}

export function countWorkspaces(group: WorkspaceGroup): number {
  return group.workspaces.length + group.children.reduce((sum, child) => sum + countWorkspaces(child), 0)
}

/**
 * The workspace ids whose rows the GROUPED render actually emits.
 *
 * Not the same as "every workspace in the tree", and the difference is a
 * selection defect rather than a cosmetic one (#2551). The grouped branch
 * renders a group's rows only when it is expanded -- `{!isCollapsed &&
 * group.workspaces.map(...)}` -- and recurses into its children under the same
 * condition, so a collapsed group hides its own rows AND its whole subtree.
 * `collapsedGroups` is persisted in `localStorage`, so this survives a reload.
 *
 * Meanwhile the page derived `visibleIds` from `sortedWorkspaces`, which is the
 * whole filtered set regardless of grouping. So select-all ticked workspaces
 * whose rows were not on screen, and because the bulk action's input is built
 * by filtering `sortedWorkspaces` -- not the rendered rows -- those workspaces
 * reached `runBulkAction`. Plan, Retry and Cancel carry no confirm, so there
 * was not even a dialog naming them.
 *
 * This mirrors the render's own two conditions exactly, including the path
 * convention (`prefix/key`, or `key` at the root) that `collapsedGroups` is
 * keyed on. `test_workspace_grouping` pins that it agrees with the component.
 */
export function renderedWorkspaceIds<T>(
  groups: readonly WorkspaceGroup<T>[],
  collapsedGroups: ReadonlySet<string>,
  pathPrefix = '',
): string[] {
  const ids: string[] = []
  for (const group of groups) {
    const fullPath = pathPrefix ? `${pathPrefix}/${group.key}` : group.key
    if (collapsedGroups.has(fullPath)) continue // its rows AND its subtree
    for (const item of group.workspaces) ids.push(item.id)
    ids.push(...renderedWorkspaceIds(group.children, collapsedGroups, fullPath))
  }
  return ids
}
