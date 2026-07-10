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
    const local: WorkspaceGroup<T> = { key: '__local__', label: 'Local', workspaces: [], children: [] }
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

function repoBasename(url: string): string {
  if (!url) return ''
  const cleaned = url.replace(/\.git$/, '')
  const lastSep = Math.max(cleaned.lastIndexOf('/'), cleaned.lastIndexOf(':'))
  return lastSep >= 0 ? cleaned.slice(lastSep + 1) : cleaned
}

function partitionByRepo<T extends WsConstraint>(workspaces: T[]) {
  const vcs = workspaces.filter(ws => ws.attributes['vcs-repo-url'])
  const local = workspaces.filter(ws => !ws.attributes['vcs-repo-url'])

  const byRepo = new Map<string, { label: string; workspaces: T[] }>()
  for (const ws of vcs) {
    const url = ws.attributes['vcs-repo-url']!
    const key = normalizeRepoUrl(url)
    if (!byRepo.has(key)) byRepo.set(key, { label: repoBasename(url), workspaces: [] })
    byRepo.get(key)!.workspaces.push(ws)
  }

  const repoGroups = Array.from(byRepo.entries())
    .sort(([, a], [, b]) => a.label.localeCompare(b.label))
    .map(([key, { label, workspaces: ws }]) => ({ key, label, workspaces: ws }))

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
