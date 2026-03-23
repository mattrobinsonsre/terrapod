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

interface VariableSet {
  id: string
  attributes: {
    name: string
    description: string
    global: boolean
    priority: boolean
    'var-count': number
    'workspace-count': number
    'created-at': string
  }
}

type VarsetSortKey = 'name' | 'description' | 'scope' | 'vars' | 'created'

export default function VariableSetsPage() {
  const router = useRouter()
  const [varsets, setVarsets] = useState<VariableSet[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [global, setGlobal] = useState(false)
  const [priority, setPriority] = useState(false)
  const [creating, setCreating] = useState(false)

  const varsetAccessor = useCallback((item: VariableSet, key: VarsetSortKey): string | number | null | undefined => {
    switch (key) {
      case 'name': return item.attributes.name
      case 'description': return item.attributes.description
      case 'scope': return item.attributes.global ? 'a-global' : item.attributes.priority ? 'b-priority' : 'c-standard'
      case 'vars': return item.attributes['var-count']
      case 'created': return item.attributes['created-at']
    }
  }, [])

  const { sortedItems, sortState, toggleSort } = useSortable<VariableSet, VarsetSortKey>(
    varsets, 'name', 'asc', varsetAccessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadVarsets()
  }, [router])

  usePollingInterval(!loading, 60_000, loadVarsets)

  async function loadVarsets() {
    setLoading(true)
    try {
      const res = await apiFetch('/api/v2/organizations/default/varsets')
      if (!res.ok) throw new Error('Failed to load variable sets')
      const data = await res.json()
      setVarsets(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load variable sets')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await apiFetch('/api/v2/organizations/default/varsets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'varsets',
            attributes: { name, description, global, priority },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create variable set (${res.status})`)
      }
      setName('')
      setDescription('')
      setGlobal(false)
      setPriority(false)
      setShowCreate(false)
      await loadVarsets()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create variable set')
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Variable Sets"
          description="Manage org-scoped variable sets for workspace groups"
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'New Variable Set'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="vs-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input id="vs-name" type="text" value={name} onChange={(e) => setName(e.target.value)} required
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_-]*"
                  title="Letters, numbers, hyphens, and underscores only. Must start with a letter or number."
                  placeholder="aws-credentials"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="vs-desc" className="block text-sm font-medium text-slate-300 mb-1">Description</label>
                <input id="vs-desc" type="text" value={description} onChange={(e) => setDescription(e.target.value)}
                  placeholder="AWS credentials for all workspaces"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            </div>
            <div className="flex gap-6">
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={global} onChange={(e) => setGlobal(e.target.checked)}
                  className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                <span className="text-sm text-slate-300">Global (apply to all workspaces)</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input type="checkbox" checked={priority} onChange={(e) => setPriority(e.target.checked)}
                  className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                <span className="text-sm text-slate-300">Priority (override workspace vars)</span>
              </label>
            </div>
            <button type="submit" disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
              {creating ? 'Creating...' : 'Create Variable Set'}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : varsets.length === 0 ? (
          <EmptyState message="No variable sets configured." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label="Name" sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Description" sortKey="description" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label="Scope" sortKey="scope" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Variables" sortKey="vars" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Created" sortKey="created" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedItems.map((vs) => (
                  <tr key={vs.id} className="hover:bg-slate-700/20 transition-colors cursor-pointer"
                    onClick={() => router.push(`/admin/variable-sets/${vs.id}`)}>
                    <td className="px-4 py-3">
                      <Link href={`/admin/variable-sets/${vs.id}`} className="text-sm font-medium text-brand-400 hover:text-brand-300"
                        onClick={(e) => e.stopPropagation()}>
                        {vs.attributes.name}
                      </Link>
                    </td>
                    <td className="px-4 py-3 text-sm text-slate-400 hidden sm:table-cell">
                      {vs.attributes.description || '-'}
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      <div className="flex gap-1">
                        {vs.attributes.global && (
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-900/50 text-blue-300">Global</span>
                        )}
                        {vs.attributes.priority && (
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">Priority</span>
                        )}
                        {!vs.attributes.global && !vs.attributes.priority && (
                          <span className="text-xs text-slate-500">Standard</span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400 hidden md:table-cell">
                      {vs.attributes['var-count'] ?? 0}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-500 hidden lg:table-cell">
                      {vs.attributes['created-at'] ? new Date(vs.attributes['created-at']).toLocaleDateString() : ''}
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
