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

interface PolicySet {
  id: string
  attributes: {
    name: string
    description: string
    'enforcement-level': string
    enabled: boolean
    'global-scope': boolean
    'policy-count': number
    'created-at': string
  }
}

type PsSortKey = 'name' | 'description' | 'enforcement' | 'scope' | 'policies' | 'created'

export default function PolicySetsPage() {
  const router = useRouter()
  const [sets, setSets] = useState<PolicySet[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [enforcement, setEnforcement] = useState('advisory')
  const [globalScope, setGlobalScope] = useState(false)
  const [creating, setCreating] = useState(false)

  const accessor = useCallback((item: PolicySet, key: PsSortKey): string | number | null | undefined => {
    switch (key) {
      case 'name': return item.attributes.name
      case 'description': return item.attributes.description
      case 'enforcement': return item.attributes['enforcement-level']
      case 'scope': return item.attributes['global-scope'] ? 'a-global' : 'b-scoped'
      case 'policies': return item.attributes['policy-count']
      case 'created': return item.attributes['created-at']
    }
  }, [])

  const { sortedItems, sortState, toggleSort } = useSortable<PolicySet, PsSortKey>(
    sets, 'name', 'asc', accessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadSets()
  }, [router])

  usePollingInterval(!loading, 60_000, loadSets)

  async function loadSets() {
    try {
      const res = await apiFetch('/api/terrapod/v1/policy-sets')
      if (!res.ok) throw new Error('Failed to load policy sets')
      const data = await res.json()
      setSets(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load policy sets')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await apiFetch('/api/terrapod/v1/policy-sets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'policy-sets',
            attributes: {
              name,
              description,
              'enforcement-level': enforcement,
              'global-scope': globalScope,
            },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create policy set (${res.status})`)
      }
      const created = await res.json()
      setName(''); setDescription(''); setEnforcement('advisory'); setGlobalScope(false)
      setShowCreate(false)
      router.push(`/admin/policy-sets/${created.data.id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create policy set')
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Policy Sets"
          description="OPA policy-as-code enforcement, scoped to workspaces by label"
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'New Policy Set'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="ps-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input id="ps-name" type="text" value={name} onChange={(e) => setName(e.target.value)} required
                  pattern="[a-zA-Z0-9][a-zA-Z0-9_\-]*"
                  title="Letters, numbers, hyphens, and underscores only. Must start with a letter or number."
                  placeholder="production-guardrails"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="ps-desc" className="block text-sm font-medium text-slate-300 mb-1">Description</label>
                <input id="ps-desc" type="text" value={description} onChange={(e) => setDescription(e.target.value)}
                  placeholder="Mandatory guardrails for production workspaces"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            </div>
            <div className="flex flex-wrap gap-6 items-center">
              <div>
                <label htmlFor="ps-enf" className="block text-sm font-medium text-slate-300 mb-1">Enforcement</label>
                <select id="ps-enf" value={enforcement} onChange={(e) => setEnforcement(e.target.value)}
                  className="px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500">
                  <option value="advisory">Advisory (warn, do not block)</option>
                  <option value="mandatory">Mandatory (block apply on failure)</option>
                </select>
              </div>
              <label className="flex items-center gap-2 cursor-pointer mt-5">
                <input type="checkbox" checked={globalScope} onChange={(e) => setGlobalScope(e.target.checked)}
                  className="rounded border-slate-600 bg-slate-700 text-brand-600 focus:ring-brand-500" />
                <span className="text-sm text-slate-300">Global (apply to every workspace)</span>
              </label>
            </div>
            <p className="text-xs text-slate-500">
              Add policies and refine label scoping after creating the set.
            </p>
            <button type="submit" disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
              {creating ? 'Creating...' : 'Create Policy Set'}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : sets.length === 0 ? (
          <EmptyState message="No policy sets configured." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <SortableHeader label="Name" sortKey="name" sortState={sortState} onSort={toggleSort} />
                  <SortableHeader label="Description" sortKey="description" sortState={sortState} onSort={toggleSort} className="hidden sm:table-cell" />
                  <SortableHeader label="Enforcement" sortKey="enforcement" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Scope" sortKey="scope" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                  <SortableHeader label="Policies" sortKey="policies" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {sortedItems.map((ps) => (
                  <tr key={ps.id} className="hover:bg-slate-700/20 transition-colors cursor-pointer"
                    onClick={() => router.push(`/admin/policy-sets/${ps.id}`)}>
                    <td className="px-4 py-3">
                      <Link href={`/admin/policy-sets/${ps.id}`} className="text-sm font-medium text-brand-400 hover:text-brand-300"
                        onClick={(e) => e.stopPropagation()}>
                        {ps.attributes.name}
                      </Link>
                      {!ps.attributes.enabled && (
                        <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-slate-700 text-slate-400">Disabled</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm text-slate-400 hidden sm:table-cell">
                      {ps.attributes.description || '-'}
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      {ps.attributes['enforcement-level'] === 'mandatory' ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-900/50 text-red-300">Mandatory</span>
                      ) : (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-900/50 text-amber-300">Advisory</span>
                      )}
                    </td>
                    <td className="px-4 py-3 hidden md:table-cell">
                      {ps.attributes['global-scope'] ? (
                        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-900/50 text-blue-300">Global</span>
                      ) : (
                        <span className="text-xs text-slate-500">Label-scoped</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400 hidden lg:table-cell">
                      {ps.attributes['policy-count'] ?? 0}
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
