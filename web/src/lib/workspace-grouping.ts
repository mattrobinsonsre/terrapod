export interface WorkspaceGroup {
  key: string
  label: string
  workspaces: WorkspaceItem[]
  children: WorkspaceGroup[]
}

export interface WorkspaceItem {
  id: string
  name: string
  workspace: unknown
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

export function buildWorkspaceTree<T extends { id: string; attributes: { name: string; 'working-directory'?: string; 'vcs-repo-url'?: string } }>(
  workspaces: T[],
  mode: GroupMode,
): WorkspaceGroup[] {
  if (mode === 'flat') return []
  if (mode === 'repo') return buildRepoTree(workspaces)
  return buildRepoPathTree(workspaces)
}

function repoBasename(url: string): string {
  if (!url) return ''
  const cleaned = url.replace(/\.git$/, '')
  const lastSep = Math.max(cleaned.lastIndexOf('/'), cleaned.lastIndexOf(':'))
  return lastSep >= 0 ? cleaned.slice(lastSep + 1) : cleaned
}

function buildPathTree<T extends { id: string; attributes: { name: string; 'working-directory'?: string; 'vcs-repo-url'?: string } }>(
  workspaces: T[],
): WorkspaceGroup[] {
  const vcsWorkspaces = workspaces.filter(ws => ws.attributes['vcs-repo-url'])
  const localWorkspaces = workspaces.filter(ws => !ws.attributes['vcs-repo-url'])

  const result: WorkspaceGroup[] = []

  const byRepo = new Map<string, T[]>()
  for (const ws of vcsWorkspaces) {
    const repo = repoBasename(ws.attributes['vcs-repo-url']!)
    if (!byRepo.has(repo)) byRepo.set(repo, [])
    byRepo.get(repo)!.push(ws)
  }

  for (const [repo, repoWs] of Array.from(byRepo.entries()).sort(([a], [b]) => a.localeCompare(b))) {
    const repoGroup: WorkspaceGroup = { key: repo, label: repo, workspaces: [], children: [] }

    for (const ws of repoWs) {
      const dir = (ws.attributes['working-directory'] || '').replace(/^\/+/, '')
      if (!dir) {
        repoGroup.workspaces.push({ id: ws.id, name: ws.attributes.name, workspace: ws })
        continue
      }

      const segments = dir.split('/').filter(Boolean)
      let current = repoGroup.children

      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i]
        let group = current.find(g => g.key === seg)
        if (!group) {
          group = { key: seg, label: seg, workspaces: [], children: [] }
          current.push(group)
        }

        if (i === segments.length - 1) {
          group.workspaces.push({ id: ws.id, name: ws.attributes.name, workspace: ws })
        } else {
          current = group.children
        }
      }
    }

    sortGroups([repoGroup])
    result.push(repoGroup)
  }

  if (localWorkspaces.length > 0) {
    const localGroup: WorkspaceGroup = { key: '__local__', label: 'Local', workspaces: [], children: [] }

    for (const ws of localWorkspaces) {
      const dir = (ws.attributes['working-directory'] || '').replace(/^\/+/, '')
      if (!dir) {
        localGroup.workspaces.push({ id: ws.id, name: ws.attributes.name, workspace: ws })
        continue
      }

      const segments = dir.split('/').filter(Boolean)
      let current = localGroup.children

      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i]
        let group = current.find(g => g.key === seg)
        if (!group) {
          group = { key: seg, label: seg, workspaces: [], children: [] }
          current.push(group)
        }

        if (i === segments.length - 1) {
          group.workspaces.push({ id: ws.id, name: ws.attributes.name, workspace: ws })
        } else {
          current = group.children
        }
      }
    }

    sortGroups([localGroup])
    result.push(localGroup)
  }

  return result
}

function sortGroups(groups: WorkspaceGroup[]): WorkspaceGroup[] {
  groups.sort((a, b) => a.label.localeCompare(b.label))
  for (const g of groups) {
    g.workspaces.sort((a, b) => a.name.localeCompare(b.name))
    g.children = sortGroups(g.children)
  }
  return groups
}

export function countWorkspaces(group: WorkspaceGroup): number {
  return group.workspaces.length + group.children.reduce((sum, child) => sum + countWorkspaces(child), 0)
}
