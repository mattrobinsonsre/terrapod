'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { useSortable } from '@/lib/use-sortable'
import { getAuthState, isAdminOrAudit } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface AuditEntry {
  id: string
  attributes: {
    'timestamp': string
    'actor-email': string
    'actor-ip': string
    'action': string
    'resource-type': string
    'resource-id': string
    'status-code': number
    'request-id': string
    'duration-ms': number
    'detail': string
  }
}

interface Pagination {
  'current-page': number
  'page-size': number
  'total-count': number
  'total-pages': number
}

function statusColor(code: number): string {
  if (code >= 200 && code < 300) return 'text-green-400'
  if (code >= 400 && code < 500) return 'text-yellow-400'
  if (code >= 500) return 'text-red-400'
  return 'text-slate-400'
}

export default function AuditLogPage() {
  const router = useRouter()
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [pagination, setPagination] = useState<Pagination | null>(null)
  const [page, setPage] = useState(1)

  // Filters
  const [filterActor, setFilterActor] = useState('')
  const [filterResourceType, setFilterResourceType] = useState('')
  const [filterAction, setFilterAction] = useState('')
  const [filterSince, setFilterSince] = useState('')
  const [filterUntil, setFilterUntil] = useState('')

  type SortKey = 'timestamp' | 'actor' | 'action' | 'resourceType' | 'resourceId' | 'status' | 'duration'
  const accessor = useCallback((item: AuditEntry, key: SortKey) => {
    switch (key) {
      case 'timestamp': return item.attributes.timestamp
      case 'actor': return item.attributes['actor-email'] || ''
      case 'action': return item.attributes.action
      case 'resourceType': return item.attributes['resource-type']
      case 'resourceId': return item.attributes['resource-id']
      case 'status': return String(item.attributes['status-code'])
      case 'duration': return String(item.attributes['duration-ms']).padStart(10, '0')
    }
  }, [])
  const { sortedItems, sortState, toggleSort } = useSortable<AuditEntry, SortKey>(
    entries, 'timestamp', 'desc', accessor,
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdminOrAudit()) { router.push('/workspaces'); return }
  }, [router])

  useEffect(() => {
    loadEntries()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page])

  async function loadEntries() {
    setLoading(true)
    setError('')
    try {
      const params = new URLSearchParams()
      params.set('page[number]', String(page))
      params.set('page[size]', '20')
      if (filterActor) params.set('filter[actor]', filterActor)
      if (filterResourceType) params.set('filter[resource-type]', filterResourceType)
      if (filterAction) params.set('filter[action]', filterAction)
      if (filterSince) params.set('filter[since]', new Date(filterSince).toISOString())
      if (filterUntil) params.set('filter[until]', new Date(filterUntil).toISOString())

      const res = await apiFetch(`/api/v2/admin/audit-log?${params.toString()}`)
      if (!res.ok) throw new Error('Failed to load audit log')
      const data = await res.json()
      setEntries(data.data || [])
      setPagination(data.meta?.pagination || null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load audit log')
    } finally {
      setLoading(false)
    }
  }

  function handleApplyFilters(e: React.FormEvent) {
    e.preventDefault()
    setPage(1)
    loadEntries()
  }

  function handleClearFilters() {
    setFilterActor('')
    setFilterResourceType('')
    setFilterAction('')
    setFilterSince('')
    setFilterUntil('')
    setPage(1)
    // Trigger reload after state update
    setTimeout(() => loadEntries(), 0)
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-7xl mx-auto">
        <PageHeader
          title="Audit Log"
          description="Immutable log of all API requests"
        />

        {error && <ErrorBanner message={error} />}

        {/* Filters */}
        <form onSubmit={handleApplyFilters} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6">
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
            <div>
              <label htmlFor="f-actor" className="block text-xs font-medium text-slate-400 mb-1">Actor Email</label>
              <input
                id="f-actor"
                type="text"
                value={filterActor}
                onChange={(e) => setFilterActor(e.target.value)}
                placeholder="user@example.com"
                className="w-full px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>
            <div>
              <label htmlFor="f-resource" className="block text-xs font-medium text-slate-400 mb-1">Resource Type</label>
              <input
                id="f-resource"
                type="text"
                value={filterResourceType}
                onChange={(e) => setFilterResourceType(e.target.value)}
                placeholder="workspaces"
                className="w-full px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>
            <div>
              <label htmlFor="f-action" className="block text-xs font-medium text-slate-400 mb-1">Method</label>
              <select
                id="f-action"
                value={filterAction}
                onChange={(e) => setFilterAction(e.target.value)}
                className="w-full px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              >
                <option value="">All</option>
                <option value="GET">GET</option>
                <option value="POST">POST</option>
                <option value="PATCH">PATCH</option>
                <option value="PUT">PUT</option>
                <option value="DELETE">DELETE</option>
              </select>
            </div>
            <div>
              <label htmlFor="f-since" className="block text-xs font-medium text-slate-400 mb-1">Since</label>
              <input
                id="f-since"
                type="datetime-local"
                value={filterSince}
                onChange={(e) => setFilterSince(e.target.value)}
                className="w-full px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>
            <div>
              <label htmlFor="f-until" className="block text-xs font-medium text-slate-400 mb-1">Until</label>
              <input
                id="f-until"
                type="datetime-local"
                value={filterUntil}
                onChange={(e) => setFilterUntil(e.target.value)}
                className="w-full px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>
          </div>
          <div className="flex gap-2 mt-3 justify-end">
            <button
              type="button"
              onClick={handleClearFilters}
              className="px-3 py-1.5 text-sm text-slate-400 hover:text-slate-200 transition-colors"
            >
              Clear
            </button>
            <button
              type="submit"
              className="px-4 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
            >
              Apply Filters
            </button>
          </div>
        </form>

        {loading ? (
          <LoadingSpinner />
        ) : entries.length === 0 ? (
          <EmptyState message="No audit log entries found." />
        ) : (
          <>
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label="Timestamp" sortKey="timestamp" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label="Actor" sortKey="actor" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label="Action" sortKey="action" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label="Resource Type" sortKey="resourceType" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label="Resource ID" sortKey="resourceId" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                      <SortableHeader label="Status" sortKey="status" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label="Duration" sortKey="duration" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {sortedItems.map((entry) => {
                      const a = entry.attributes
                      return (
                        <tr key={entry.id} className="hover:bg-slate-700/20 transition-colors">
                          <td className="px-4 py-2.5 text-xs text-slate-300 whitespace-nowrap">
                            {new Date(a.timestamp).toLocaleString()}
                          </td>
                          <td className="px-4 py-2.5 text-sm text-slate-300">
                            {a['actor-email'] || <span className="text-slate-600">-</span>}
                          </td>
                          <td className="px-4 py-2.5">
                            <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-mono font-medium bg-slate-700/50 text-slate-300">
                              {a.action}
                            </span>
                          </td>
                          <td className="px-4 py-2.5 text-sm text-slate-400">{a['resource-type']}</td>
                          <td className="px-4 py-2.5 text-xs text-slate-500 font-mono hidden lg:table-cell">
                            {a['resource-id'] || '-'}
                          </td>
                          <td className="px-4 py-2.5">
                            <span className={`text-sm font-mono font-medium ${statusColor(a['status-code'])}`}>
                              {a['status-code']}
                            </span>
                          </td>
                          <td className="px-4 py-2.5 text-xs text-slate-500 hidden md:table-cell">
                            {a['duration-ms']}ms
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            {/* Pagination */}
            {pagination && pagination['total-pages'] > 1 && (
              <div className="flex items-center justify-between mt-4">
                <span className="text-sm text-slate-400">
                  Page {pagination['current-page']} of {pagination['total-pages']} ({pagination['total-count']} entries)
                </span>
                <div className="flex gap-2">
                  <button
                    onClick={() => setPage(Math.max(1, page - 1))}
                    disabled={page <= 1}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 disabled:bg-slate-800 disabled:text-slate-600 text-slate-200 transition-colors"
                  >
                    Previous
                  </button>
                  <button
                    onClick={() => setPage(page + 1)}
                    disabled={page >= pagination['total-pages']}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 disabled:bg-slate-800 disabled:text-slate-600 text-slate-200 transition-colors"
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </main>
    </>
  )
}
