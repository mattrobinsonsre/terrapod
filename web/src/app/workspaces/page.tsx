'use client'

import { Suspense, useEffect, useRef, useState, useCallback, useMemo } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
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
import {
  hasStatusTerm,
  matchWorkspace,
  parseFilterQuery,
  removeTerm,
  serializeFilter,
  toggleStatusTerm,
} from '@/lib/workspace-filter'
import { WORKSPACE_STATUSES, resolveStatus } from '@/lib/workspace-status'

interface LatestRun {
  id: string
  status: string
  'plan-only': boolean
  'created-at': string
}

interface HealthCondition {
  code: string
  severity: string
  title: string
  detail: string
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
    'agent-pool-name': string | null
    'drift-detection-enabled': boolean
    'drift-status': string
    'state-diverged': boolean
    'vcs-last-error': string | null
    'health-conditions': HealthCondition[]
    'latest-run': LatestRun | null
    'created-at': string
    labels?: Record<string, string> | null
  }
}

// Wrapped in <Suspense> at the bottom — `useSearchParams()` triggers Next.js's
// CSR bailout, and without a boundary the whole page fails to build statically.
function WorkspacesPageInner() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const [workspaces, setWorkspaces] = useState<Workspace[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Filter input. Tokens are space-separated; bare words match the workspace
  // name (case-insensitive substring), `key:value` / `key=value` match a
  // label exactly, and `key:` / `key=` matches any workspace that has the
  // label key set. Mirrored to the URL via ?q=… so refresh + share work.
  const [filterInput, setFilterInput] = useState(searchParams.get('q') || '')
  const parsedFilter = useMemo(() => parseFilterQuery(filterInput), [filterInput])

  // Status dropdown state. Closes on outside-click and Escape — same pattern
  // used for the run-actions menu so the page feels consistent.
  const [statusMenuOpen, setStatusMenuOpen] = useState(false)
  const statusMenuRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!statusMenuOpen) return
    const onClick = (e: MouseEvent) => {
      if (statusMenuRef.current && !statusMenuRef.current.contains(e.target as Node)) {
        setStatusMenuOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setStatusMenuOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [statusMenuOpen])

  // Sync the input to the URL whenever the parsed filter changes.
  //
  // Dedup via `lastSyncedQueryRef` — we track the last query string we
  // wrote and skip redundant `router.replace` calls. We deliberately do
  // NOT compare against `searchParams.get('q')`: `useSearchParams()` is
  // updated by Next.js asynchronously after `router.replace`, so its
  // value in the effect closure can be one render behind the URL we
  // just wrote. That stale read was the cause of the workspaces filter
  // sync being "hit and miss" — it would early-return on a comparison
  // against an out-of-date `current`, leaving the address bar stuck.
  // The ref is written-once-per-real-update and never reads back from
  // the URL, so it can't go stale.
  //
  // Initialised to the URL's current `q` so that mounting on a page
  // already loaded with `?q=...` (refresh, shared link) doesn't fire a
  // redundant `router.replace` over the unchanged URL on first render.
  const lastSyncedQueryRef = useRef<string | null>(searchParams.get('q') || '')
  useEffect(() => {
    const serialized = serializeFilter(parsedFilter)
    if (lastSyncedQueryRef.current === serialized) return
    lastSyncedQueryRef.current = serialized
    const url = serialized
      ? `/workspaces?q=${encodeURIComponent(serialized)}`
      : '/workspaces'
    router.replace(url, { scroll: false })
  }, [parsedFilter, router])

  const filteredWorkspaces = useMemo(() => {
    if (parsedFilter.terms.length === 0) return workspaces
    return workspaces.filter(ws => {
      // Status terms need the resolved status; pass it conditionally so
      // workspaces without a status (— display) don't accidentally match
      // an empty `status:` predicate.
      return matchWorkspace(ws, parsedFilter, resolveStatus(ws).def?.filter ?? undefined)
    })
  }, [workspaces, parsedFilter])

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
  const [newAgentPoolId, setNewAgentPoolId] = useState('')
  const [creating, setCreating] = useState(false)

  // Version suggestions
  const [versionSuggestions, setVersionSuggestions] = useState<string[]>([])
  const [versionsBackend, setVersionsBackend] = useState('')

  // VCS connections
  const [vcsConnections, setVcsConnections] = useState<{ id: string; attributes: { name: string; provider: string } }[]>([])
  const [vcsConnectionsLoaded, setVcsConnectionsLoaded] = useState(false)

  // Agent pools
  const [agentPools, setAgentPools] = useState<{ id: string; attributes: { name: string } }[]>([])
  const [agentPoolsLoaded, setAgentPoolsLoaded] = useState(false)

  type WsSortKey = 'name' | 'mode' | 'pool' | 'resources' | 'status' | 'created'

  // resolveStatus + WORKSPACE_STATUSES are imported from workspace-status.ts
  // — single source of truth for both the row pill and the filter dropdown.

  // Per-status counts on the unfiltered workspace list, memoised so the
  // dropdown render is a constant lookup. Recomputes only when the
  // workspace data changes.
  const statusCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const ws of workspaces) {
      const f = resolveStatus(ws).def?.filter
      if (f) counts[f] = (counts[f] || 0) + 1
    }
    return counts
  }, [workspaces])

  const badgeColors: Record<string, string> = {
    amber: 'bg-amber-900/50 text-amber-300',
    red: 'bg-red-900/50 text-red-300',
    blue: 'bg-blue-900/50 text-blue-300',
    green: 'bg-green-900/50 text-green-300',
    slate: 'bg-slate-700/50 text-slate-400',
    gray: 'text-slate-500',
  }

  const { sortedItems: sortedWorkspaces, sortState, toggleSort } = useSortable<Workspace, WsSortKey>(
    filteredWorkspaces, 'name', 'asc',
    useCallback((item: Workspace, key: WsSortKey) => {
      switch (key) {
        case 'name': return item.attributes.name
        case 'mode': return item.attributes['execution-mode']
        case 'pool': return item.attributes['agent-pool-name'] || ''
        case 'resources': return item.attributes['resource-cpu']
        case 'status': return resolveStatus(item).def?.label ?? '\u2014'
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

  // Load VCS connections and agent pools when form opens
  useEffect(() => {
    if (!showCreate) return
    if (!vcsConnectionsLoaded) {
      apiFetch('/api/v2/organizations/default/vcs-connections')
        .then(res => res.ok ? res.json() : { data: [] })
        .then(data => { setVcsConnections(data.data || []); setVcsConnectionsLoaded(true) })
        .catch(() => {})
    }
    if (!agentPoolsLoaded) {
      apiFetch('/api/v2/organizations/default/agent-pools')
        .then(res => res.ok ? res.json() : { data: [] })
        .then(data => { setAgentPools(data.data || []); setAgentPoolsLoaded(true) })
        .catch(() => {})
    }
  }, [showCreate, vcsConnectionsLoaded, agentPoolsLoaded])

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
              ...(newAgentPoolId ? { 'agent-pool-id': newAgentPoolId } : {}),
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
      setNewAgentPoolId('')
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
                  <option value="agent">Agent</option>
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
              {newExecMode === 'agent' && (
              <div>
                <label htmlFor="ws-pool" className="block text-sm font-medium text-slate-300 mb-1">Agent Pool</label>
                <select
                  id="ws-pool"
                  value={newAgentPoolId}
                  onChange={(e) => setNewAgentPoolId(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="">None</option>
                  {agentPools.map((p) => (
                    <option key={p.id} value={p.id}>{p.attributes.name}</option>
                  ))}
                </select>
              </div>
              )}
              <div>
                <span className="block text-sm font-medium text-slate-300 mb-1">Auto Apply</span>
                <label className="flex items-center gap-2 h-[42px] px-3 cursor-pointer border border-slate-600 rounded-lg bg-slate-700">
                  <input
                    type="checkbox"
                    checked={newAutoApply}
                    onChange={(e) => setNewAutoApply(e.target.checked)}
                    className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500"
                  />
                  <span className="text-sm text-slate-300">Enabled</span>
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

        {!loading && workspaces.length > 0 && (() => {
          const total = workspaces.length
          const withConditions = workspaces.filter(ws => (ws.attributes['health-conditions'] || []).length > 0).length
          const locked = workspaces.filter(ws => ws.attributes.locked).length
          return (
            <div className="grid grid-cols-3 gap-4 mb-6">
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
                <p className="text-xs text-slate-500 uppercase tracking-wider">Total</p>
                <p className="text-2xl font-semibold text-slate-100 mt-1">{total}</p>
              </div>
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
                <p className="text-xs text-slate-500 uppercase tracking-wider">Health Issues</p>
                <p className={`text-2xl font-semibold mt-1 ${withConditions > 0 ? 'text-red-400' : 'text-slate-100'}`}>{withConditions}</p>
              </div>
              <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4">
                <p className="text-xs text-slate-500 uppercase tracking-wider">Locked</p>
                <p className={`text-2xl font-semibold mt-1 ${locked > 0 ? 'text-amber-400' : 'text-slate-100'}`}>{locked}</p>
              </div>
            </div>
          )
        })()}

        {!loading && workspaces.length > 0 && (() => {
          const activeStatusCount = parsedFilter.terms.filter(t => t.kind === 'status').length
          return (
            <div className="mb-4">
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={filterInput}
                  onChange={e => setFilterInput(e.target.value)}
                  placeholder='Filter by name, label, or status — e.g. "eu1", "env:prod", "status:errored"'
                  aria-label="Filter workspaces"
                  className="flex-1 px-3 py-2 rounded-lg bg-slate-800/50 border border-slate-700/50 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-brand-500"
                />
                {/* Status dropdown — single entry point for all status presets.
                    Picks any combination via toggle; the existing chips below
                    show what's active and let the user remove individually. */}
                <div className="relative" ref={statusMenuRef}>
                  <button
                    type="button"
                    aria-haspopup="menu"
                    aria-expanded={statusMenuOpen}
                    onClick={() => setStatusMenuOpen(o => !o)}
                    className={
                      'inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium border transition-colors ' +
                      (activeStatusCount > 0
                        ? 'bg-slate-700/60 text-slate-100 border-slate-600'
                        : 'bg-slate-800/50 text-slate-300 border-slate-700/50 hover:bg-slate-700/60')
                    }
                  >
                    <span>Status</span>
                    {activeStatusCount > 0 && (
                      <span className="inline-flex items-center justify-center min-w-5 px-1.5 rounded-full text-[10px] font-semibold bg-brand-600 text-white">
                        {activeStatusCount}
                      </span>
                    )}
                    <svg className={'w-3 h-3 transition-transform ' + (statusMenuOpen ? 'rotate-180' : '')} viewBox="0 0 12 12" fill="currentColor" aria-hidden>
                      <path d="M3 4.5l3 3 3-3" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </button>
                  {statusMenuOpen && (
                    <div
                      role="menu"
                      className="absolute right-0 z-10 mt-1 w-64 rounded-lg bg-slate-800 border border-slate-700 shadow-xl py-1 max-h-96 overflow-y-auto"
                    >
                      {WORKSPACE_STATUSES.map(opt => {
                        const active = hasStatusTerm(parsedFilter, opt.filter)
                        const count = statusCounts[opt.filter] || 0
                        return (
                          <button
                            key={opt.filter}
                            type="button"
                            role="menuitemcheckbox"
                            aria-checked={active}
                            onClick={() => setFilterInput(serializeFilter(toggleStatusTerm(parsedFilter, opt.filter)))}
                            className={
                              'w-full flex items-center gap-2 px-3 py-1.5 text-sm transition-colors ' +
                              (active ? 'bg-slate-700/60 text-slate-100' : 'text-slate-300 hover:bg-slate-700/40')
                            }
                          >
                            {/* Checkmark rail keeps the option labels aligned whether
                                checked or not. */}
                            <span className="w-3 inline-flex justify-center text-brand-400">
                              {active ? '✓' : ''}
                            </span>
                            <span className={'w-1.5 h-1.5 rounded-full ' + opt.dot} />
                            <span className="flex-1 text-left">{opt.label}</span>
                            <span className={'text-xs ' + (count > 0 ? 'text-slate-400' : 'text-slate-600')}>{count}</span>
                          </button>
                        )
                      })}
                    </div>
                  )}
                </div>
                {parsedFilter.terms.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setFilterInput('')}
                    className="px-3 py-2 rounded-lg text-sm text-slate-400 hover:text-slate-200 transition-colors"
                  >
                    Clear
                  </button>
                )}
              </div>
              {parsedFilter.terms.length > 0 && (
                <div className="flex flex-wrap gap-2 mt-2">
                  {parsedFilter.terms.map((term, i) => {
                    const label =
                      term.kind === 'name'
                        ? `name: ${term.value}`
                        : term.kind === 'status'
                          ? `status: ${term.value}`
                          : term.value === null
                            ? `${term.key}: (any)`
                            : `${term.key}: ${term.value}`
                    return (
                      <button
                        type="button"
                        key={`${i}-${label}`}
                        onClick={() => setFilterInput(serializeFilter(removeTerm(parsedFilter, i)))}
                        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-slate-700/50 text-slate-300 hover:bg-slate-700 transition-colors"
                        title="Remove this term"
                      >
                        <span>{label}</span>
                        <span aria-hidden className="text-slate-500">×</span>
                      </button>
                    )
                  })}
                  <span className="text-xs text-slate-500 self-center">
                    Showing {filteredWorkspaces.length} of {workspaces.length}
                  </span>
                </div>
              )}
            </div>
          )
        })()}

        {loading ? (
          <LoadingSpinner />
        ) : workspaces.length === 0 ? (
          <EmptyState message="No workspaces yet. Create one to get started." />
        ) : filteredWorkspaces.length === 0 ? (
          <EmptyState message="No workspaces match this filter." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label="Name" sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Mode" sortKey="mode" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label="Pool" sortKey="pool" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Resources" sortKey="resources" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <SortableHeader label="Status" sortKey="status" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                  <SortableHeader label="Created" sortKey="created" sortState={sortState} onSort={toggleSort} className="hidden xl:table-cell" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedWorkspaces.map((ws) => {
                  const { def, runId } = resolveStatus(ws)
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
                        {ws.attributes['agent-pool-name'] || '\u2014'}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      <span className="text-xs text-slate-400">
                        {ws.attributes['resource-cpu']} CPU / {ws.attributes['resource-memory']}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden lg:table-cell">
                      {!def ? (
                        <span className="text-xs text-slate-500">&mdash;</span>
                      ) : runId ? (
                        <Link
                          href={`/workspaces/${ws.id}/runs/${runId}`}
                          className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium hover:opacity-80 transition-opacity ${badgeColors[def.color]}`}
                        >
                          {def.label}
                        </Link>
                      ) : (
                        <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${badgeColors[def.color]}`}>
                          {def.label}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 hidden xl:table-cell">
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

export default function WorkspacesPage() {
  return (
    <Suspense fallback={<><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>}>
      <WorkspacesPageInner />
    </Suspense>
  )
}
