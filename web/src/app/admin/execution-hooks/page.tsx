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
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { usePollingInterval } from '@/lib/use-polling-interval'

const HOOK_POINTS = ['pre_init', 'pre_plan', 'post_plan', 'pre_apply', 'post_apply'] as const

interface ExecutionHook {
  id: string
  attributes: {
    name: string
    description: string
    'hook-point': string
    script: string
    enabled: boolean
    priority: number
    'workspace-count': number
    'created-at': string
  }
}

type HookSortKey = 'name' | 'point' | 'enabled' | 'priority' | 'workspaces'

export default function ExecutionHooksPage() {
  const router = useRouter()
  const [hooks, setHooks] = useState<ExecutionHook[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [hookPoint, setHookPoint] = useState<string>('pre_init')
  const [script, setScript] = useState('')
  const [priority, setPriority] = useState(0)
  const [creating, setCreating] = useState(false)

  const hookAccessor = useCallback(
    (item: ExecutionHook, key: HookSortKey): string | number | null | undefined => {
      switch (key) {
        case 'name': return item.attributes.name
        case 'point': return item.attributes['hook-point']
        case 'enabled': return item.attributes.enabled ? 'a-enabled' : 'b-disabled'
        case 'priority': return item.attributes.priority
        case 'workspaces': return item.attributes['workspace-count']
      }
    },
    [],
  )

  const { sortedItems, sortState, toggleSort } = useSortable<ExecutionHook, HookSortKey>(
    hooks, 'name', 'asc', hookAccessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadHooks()
  }, [router])

  usePollingInterval(!loading, 60_000, loadHooks)

  async function loadHooks() {
    try {
      const res = await apiFetch('/api/terrapod/v1/execution-hooks')
      if (!res.ok) throw new Error('Failed to load execution hooks')
      const data = await res.json()
      setHooks(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load execution hooks')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await apiFetch('/api/terrapod/v1/execution-hooks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'execution-hooks',
            attributes: {
              name,
              description,
              'hook-point': hookPoint,
              script,
              priority: Number(priority) || 0,
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create execution hook (${res.status})`)
      }
      setName('')
      setDescription('')
      setHookPoint('pre_init')
      setScript('')
      setPriority(0)
      setShowCreate(false)
      await loadHooks()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create execution hook')
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Execution Hooks"
          description="Custom shell steps run inside runner Jobs at pre/post-plan/apply points, associated with workspaces"
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'New Execution Hook'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="hook-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input id="hook-name" type="text" value={name} onChange={(e) => setName(e.target.value)} required
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                  title="Letters, numbers, hyphens, and underscores only. Must start with a letter or number."
                  placeholder="etc-hosts-entry"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="hook-desc" className="block text-sm font-medium text-slate-300 mb-1">Description</label>
                <input id="hook-desc" type="text" value={description} onChange={(e) => setDescription(e.target.value)}
                  placeholder="Add an internal hosts entry before init"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="hook-point" className="block text-sm font-medium text-slate-300 mb-1">Hook Point</label>
                <select id="hook-point" value={hookPoint} onChange={(e) => setHookPoint(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent">
                  {HOOK_POINTS.map((p) => <option key={p} value={p}>{p}</option>)}
                </select>
              </div>
              <div>
                <label htmlFor="hook-priority" className="block text-sm font-medium text-slate-300 mb-1">Priority</label>
                <input id="hook-priority" type="number" value={priority} onChange={(e) => setPriority(Number(e.target.value))}
                  title="Lower runs first when several hooks share a point (ties broken by name)."
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            </div>
            <div>
              <label htmlFor="hook-script" className="block text-sm font-medium text-slate-300 mb-1">Script (<code>/bin/sh -c</code>)</label>
              <textarea id="hook-script" value={script} onChange={(e) => setScript(e.target.value)} rows={4}
                placeholder="echo '10.0.0.5 registry.internal' >> /etc/hosts"
                className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent resize-y" />
              <p className="mt-1 text-xs text-slate-500">Runs with the runner&apos;s cloud identity. A non-zero exit fails the run. Secrets should come from workspace variables, not inline here.</p>
            </div>
            <button type="submit" disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
              {creating ? 'Creating...' : 'Create Execution Hook'}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : hooks.length === 0 ? (
          <EmptyState message="No execution hooks configured." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label="Name" sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Hook Point" sortKey="point" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label="Enabled" sortKey="enabled" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Priority" sortKey="priority" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Workspaces" sortKey="workspaces" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedItems.map((h) => (
                  <tr key={h.id} className="hover:bg-slate-700/20 transition-colors cursor-pointer"
                    onClick={() => router.push(`/admin/execution-hooks/${h.id}`)}>
                    <td className="px-4 py-3">
                      <Link href={`/admin/execution-hooks/${h.id}`} className="text-sm font-medium text-brand-400 hover:text-brand-300"
                        onClick={(e) => e.stopPropagation()}>
                        {h.attributes.name}
                      </Link>
                      {h.attributes.description && (
                        <div className="text-xs text-slate-500 mt-0.5">{h.attributes.description}</div>
                      )}
                    </td>
                    <td className="px-4 py-3 hidden sm:table-cell">
                      <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-purple-900/50 text-purple-300 font-mono">
                        {h.attributes['hook-point']}
                      </span>
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      {h.attributes.enabled ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-900/50 text-green-300">Enabled</span>
                      ) : (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700 text-slate-400">Disabled</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400 hidden md:table-cell">
                      {h.attributes.priority ?? 0}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400 hidden lg:table-cell">
                      {h.attributes['workspace-count'] ?? 0}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </main>
    </>
  )
}
