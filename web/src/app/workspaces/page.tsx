'use client'

import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { useWorkspaceListEvents } from '@/lib/use-workspace-list-events'

interface LatestRun {
  id: string
  status: string
  'plan-only': boolean
  'created-at': string
}

interface Workspace {
  id: string
  attributes: {
    name: string
    'execution-mode': string
    'auto-apply': boolean
    'terraform-version': string
    locked: boolean
    'resource-cpu': string
    'resource-memory': string
    'drift-detection-enabled': boolean
    'drift-status': string
    'state-diverged': boolean
    'latest-run': LatestRun | null
    'created-at': string
  }
}

export default function WorkspacesPage() {
  const router = useRouter()
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newExecMode, setNewExecMode] = useState('local')
  const [newAutoApply, setNewAutoApply] = useState(false)
  const [newBackend, setNewBackend] = useState('tofu')
  const [newVersion, setNewVersion] = useState('1.11')
  const [newCpu, setNewCpu] = useState('1')
  const [newMemory, setNewMemory] = useState('2Gi')
  const [newWorkingDir, setNewWorkingDir] = useState('')
  const [newVcsConnectionId, setNewVcsConnectionId] = useState('')
  const [newVcsRepoUrl, setNewVcsRepoUrl] = useState('')
  const [newVcsBranch, setNewVcsBranch] = useState('')
  const [creating, setCreating] = useState(false)

  // Version suggestions
  const [versionSuggestions, setVersionSuggestions] = useState<string[]>([])
  const [versionsBackend, setVersionsBackend] = useState('')

  // VCS connections
  const [vcsConnections, setVcsConnections] = useState<{ id: string; attributes: { name: string; provider: string } }[]>([])
  const [vcsConnectionsLoaded, setVcsConnectionsLoaded] = useState(false)

  type WsSortKey = 'name' | 'mode' | 'resources' | 'status' | 'created'

  function resolveStatus(ws: Workspace): { label: string; color: string; priority: number } {
    const drift = ws.attributes['drift-status']
    const run = ws.attributes['latest-run']

    if (ws.attributes['state-diverged']) return { label: 'State Diverged', color: 'red', priority: 0 }
    if (drift === 'drifted') return { label: 'Drifted', color: 'amber', priority: 1 }
    if (run) {
      const s = run.status
      const planOnly = run['plan-only']
      if (s === 'errored') return { label: 'Errored', color: 'red', priority: 2 }
      if (s === 'planned' && !planOnly) return { label: 'Needs Confirm', color: 'amber', priority: 3 }
      if (s === 'planning') return { label: 'Planning', color: 'blue', priority: 4 }
      if (s === 'applying') return { label: 'Applying', color: 'blue', priority: 4 }
      if (s === 'confirmed') return { label: 'Confirmed', color: 'blue', priority: 4 }
      if (s === 'queued') return { label: 'Queued', color: 'blue', priority: 4 }
      if (s === 'pending') return { label: 'Pending', color: 'slate', priority: 5 }
      if (s === 'applied') return { label: 'Applied', color: 'green', priority: 6 }
      if (s === 'planned' && planOnly) return { label: 'Planned', color: 'green', priority: 7 }
      if (s === 'canceled') return { label: 'Canceled', color: 'slate', priority: 8 }
      if (s === 'discarded') return { label: 'Discarded', color: 'slate', priority: 8 }
    }
    return { label: '\u2014', color: 'gray', priority: 9 }
  }

  const badgeColors: Record<string, string> = {
    amber: 'bg-amber-900/50 text-amber-300',
    red: 'bg-red-900/50 text-red-300',
    blue: 'bg-blue-900/50 text-blue-300',
    green: 'bg-green-900/50 text-green-300',
    slate: 'bg-slate-700/50 text-slate-400',
    gray: 'text-slate-500',
  }

  const { sortedItems: sortedWorkspaces, sortState, toggleSort } = useSortable<Workspace, WsSortKey>(
    workspaces, 'name', 'asc',
    useCallback((item: Workspace, key: WsSortKey) => {
      switch (key) {
        case 'name': return item.attributes.name
        case 'mode': return item.attributes['execution-mode']
        case 'resources': return item.attributes['resource-cpu']
        case 'status': return resolveStatus(item).label
        case 'created': return item.attributes['created-at']
      }
    }, []),
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadWorkspaces()
  }, [router])

  // Real-time workspace list updates via SSE
  useWorkspaceListEvents(workspaces.length > 0, useCallback(() => {
    loadWorkspaces()
  }, []))

  // Load VCS connections when form opens
  useEffect(() => {
    if (!showCreate || vcsConnectionsLoaded) return
    apiFetch('/api/v2/organizations/default/vcs-connections')
      .then(res => res.ok ? res.json() : { data: [] })
      .then(data => { setVcsConnections(data.data || []); setVcsConnectionsLoaded(true) })
      .catch(() => {})
  }, [showCreate, vcsConnectionsLoaded])

  // Fetch version suggestions when backend changes and form is open
  useEffect(() => {
    if (!showCreate || newBackend === versionsBackend) return
    apiFetch(`/api/v2/binary-cache/versions?tool=${newBackend}`)
      .then(res => res.ok ? res.json() : { data: [] })
      .then(data => {
        setVersionSuggestions(data.data || [])
        setVersionsBackend(newBackend)
      })
      .catch(() => {})
  }, [showCreate, newBackend, versionsBackend])

  async function loadWorkspaces() {
    setLoading(true)
    try {
      const res = await apiFetch('/api/v2/organizations/default/workspaces')
      if (!res.ok) throw new Error('Failed to load workspaces')
      const data = await res.json()
      setWorkspaces(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load workspaces')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await apiFetch('/api/v2/organizations/default/workspaces', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'workspaces',
            attributes: {
              name: newName,
              'execution-mode': newExecMode,
              'execution-backend': newBackend,
              'terraform-version': newVersion,
              'auto-apply': newAutoApply,
              'resource-cpu': newCpu,
              'resource-memory': newMemory,
              'working-directory': newWorkingDir,
              'vcs-repo-url': newVcsRepoUrl,
              'vcs-branch': newVcsBranch,
            },
            ...(newVcsConnectionId ? {
              relationships: {
                'vcs-connection': {
                  data: { id: newVcsConnectionId, type: 'vcs-connections' },
                },
              },
            } : {}),
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create workspace (${res.status})`)
      }
      setNewName('')
      setNewExecMode('local')
      setNewBackend('tofu')
      setNewVersion('1.11')
      setNewAutoApply(false)
      setNewCpu('1')
      setNewMemory('2Gi')
      setNewWorkingDir('')
      setNewVcsConnectionId('')
      setNewVcsRepoUrl('')
      setNewVcsBranch('')
      setShowCreate(false)
      await loadWorkspaces()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create workspace')
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Workspaces"
          description="Manage Terraform workspaces, state, and runs"
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'New Workspace'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              <div>
                <label htmlFor="ws-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input
                  id="ws-name"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_-]*"
                  title="Letters, numbers, hyphens, and underscores only. Must start with a letter or number."
                  placeholder="my-workspace"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="ws-exec" className="block text-sm font-medium text-slate-300 mb-1">Execution Mode</label>
                <select
                  id="ws-exec"
                  value={newExecMode}
                  onChange={(e) => setNewExecMode(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="local">Local</option>
                  <option value="remote">Remote</option>
                </select>
              </div>
              <div>
                <label htmlFor="ws-backend" className="block text-sm font-medium text-slate-300 mb-1">Execution Backend</label>
                <select
                  id="ws-backend"
                  value={newBackend}
                  onChange={(e) => setNewBackend(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="tofu">OpenTofu</option>
                  <option value="terraform">Terraform</option>
                </select>
              </div>
              <div>
                <label htmlFor="ws-version" className="block text-sm font-medium text-slate-300 mb-1">Version</label>
                <input
                  id="ws-version"
                  type="text"
                  list="version-suggestions"
                  value={newVersion}
                  onChange={(e) => setNewVersion(e.target.value)}
                  pattern="[0-9]+\.[0-9]+(\.[0-9]+)?"
                  title="Version in X.Y or X.Y.Z format (e.g. 1.11 or 1.11.5)"
                  placeholder="e.g. 1.11 or 1.11.5"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
                <datalist id="version-suggestions">
                  {versionSuggestions.map(v => (
                    <option key={v} value={v} />
                  ))}
                </datalist>
              </div>
              <div>
                <label htmlFor="ws-cpu" className="block text-sm font-medium text-slate-300 mb-1">CPU Request</label>
                <input
                  id="ws-cpu"
                  type="text"
                  value={newCpu}
                  onChange={(e) => setNewCpu(e.target.value)}
                  pattern="[0-9]+m|[0-9]+(\.[0-9]+)?"
                  title="Kubernetes CPU quantity: whole cores (1, 2) or millicores (500m, 100m)"
                  placeholder="1"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
                <p className="mt-1 text-xs text-slate-500">e.g. 1, 0.5, 500m</p>
              </div>
              <div>
                <label htmlFor="ws-mem" className="block text-sm font-medium text-slate-300 mb-1">Memory Request</label>
                <input
                  id="ws-mem"
                  type="text"
                  value={newMemory}
                  onChange={(e) => setNewMemory(e.target.value)}
                  pattern="[0-9]+(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E|m)?"
                  title="Kubernetes memory quantity: bytes (1000) or with suffix (512Mi, 2Gi, 1Ti)"
                  placeholder="2Gi"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
                <p className="mt-1 text-xs text-slate-500">e.g. 2Gi, 512Mi, 1Ti</p>
              </div>
              <div className="flex items-end">
                <label className="flex items-center gap-2 px-3 py-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={newAutoApply}
                    onChange={(e) => setNewAutoApply(e.target.checked)}
                    className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                  />
                  <span className="text-sm text-slate-300">Auto Apply</span>
                </label>
              </div>
              <div>
                <label htmlFor="ws-workdir" className="block text-sm font-medium text-slate-300 mb-1">Working Directory</label>
                <input
                  id="ws-workdir"
                  type="text"
                  value={newWorkingDir}
                  onChange={(e) => setNewWorkingDir(e.target.value)}
                  placeholder="e.g. environments/dev"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
                <p className="mt-1 text-xs text-slate-500">Subdirectory within repo containing .tf files</p>
              </div>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              <div>
                <label htmlFor="ws-vcs-conn" className="block text-sm font-medium text-slate-300 mb-1">VCS Connection</label>
                <select
                  id="ws-vcs-conn"
                  value={newVcsConnectionId}
                  onChange={(e) => setNewVcsConnectionId(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="">None</option>
                  {vcsConnections.map((c) => (
                    <option key={c.id} value={c.id}>{c.attributes.name} ({c.attributes.provider})</option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="ws-vcs-repo" className="block text-sm font-medium text-slate-300 mb-1">VCS Repository URL</label>
                <input
                  id="ws-vcs-repo"
                  type="text"
                  value={newVcsRepoUrl}
                  onChange={(e) => setNewVcsRepoUrl(e.target.value)}
                  pattern="https?://.+"
                  title="Must be an HTTP or HTTPS URL"
                  placeholder="https://github.com/org/repo"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="ws-vcs-branch" className="block text-sm font-medium text-slate-300 mb-1">VCS Branch</label>
                <input
                  id="ws-vcs-branch"
                  type="text"
                  value={newVcsBranch}
                  onChange={(e) => setNewVcsBranch(e.target.value)}
                  placeholder="main (default)"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
            </div>
            <button
              type="submit"
              disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {creating ? 'Creating...' : 'Create Workspace'}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : workspaces.length === 0 ? (
          <EmptyState message="No workspaces yet. Create one to get started." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label="Name" sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Mode" sortKey="mode" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label="Resources" sortKey="resources" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Status" sortKey="status" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <SortableHeader label="Created" sortKey="created" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedWorkspaces.map((ws) => {
                  const status = resolveStatus(ws)
                  return (
                  <tr key={ws.id} className="hover:bg-slate-700/20 transition-colors">
                    <td className="px-4 py-3">
                      <Link
                        href={`/workspaces/${ws.id}`}
                        className="text-sm font-medium text-brand-400 hover:text-brand-300"
                      >
                        {ws.attributes.name}
                      </Link>
                    </td>
                    <td className="px-4 py-3 hidden sm:table-cell">
                      <span className="text-xs text-slate-400">{ws.attributes['execution-mode']}</span>
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      <span className="text-xs text-slate-400">
                        {ws.attributes['resource-cpu']} CPU / {ws.attributes['resource-memory']}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      {status.color === 'gray' ? (
                        <span className="text-xs text-slate-500">{status.label}</span>
                      ) : (
                        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${badgeColors[status.color]}`}>
                          {status.label}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <span className="text-xs text-slate-500">
                        {ws.attributes['created-at'] ? new Date(ws.attributes['created-at']).toLocaleDateString() : ''}
                      </span>
                    </td>
                  </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </>
  )
}
