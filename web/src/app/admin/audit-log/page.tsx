'use client'

import { useCallback, useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { useSortable } from '@/lib/use-sortable'
import { usePollingInterval } from '@/lib/use-polling-interval'
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
  const t = useTranslations('adminAuditLog')
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

  usePollingInterval(!loading, 30_000, loadEntries)

  async function loadEntries(overrides?: {
    actor?: string
    resourceType?: string
    action?: string
    since?: string
    until?: string
    page?: number
  }) {
    setError('')
    try {
      // Allow callers (e.g. Clear) to pass freshly-computed filter values so
      // the fetch doesn't race the async state setters.
      const actor = overrides?.actor ?? filterActor
      const resourceType = overrides?.resourceType ?? filterResourceType
      const action = overrides?.action ?? filterAction
      const since = overrides?.since ?? filterSince
      const until = overrides?.until ?? filterUntil
      const pageNum = overrides?.page ?? page
      const params = new URLSearchParams()
      params.set('page[number]', String(pageNum))
      params.set('page[size]', '20')
      if (actor) params.set('filter[actor]', actor)
      if (resourceType) params.set('filter[resource-type]', resourceType)
      if (action) params.set('filter[action]', action)
      if (since) params.set('filter[since]', new Date(since).toISOString())
      if (until) params.set('filter[until]', new Date(until).toISOString())

      const res = await apiFetch(`/api/terrapod/v1/admin/audit-log?${params.toString()}`)
      if (!res.ok) throw new Error(t('errors.load'))
      const data = await res.json()
      setEntries(data.data || [])
      setPagination(data.meta?.pagination || null)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('errors.load'))
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
    // Reload with the cleared filters EXPLICITLY. The state setters above are
    // async, so a deferred loadEntries() would close over the pre-clear filter
    // values and reload the still-filtered (often empty) list — the clear
    // would appear to do nothing when the prior filter had no matches.
    loadEntries({ actor: '', resourceType: '', action: '', since: '', until: '', page: 1 })
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-7xl mx-auto">
        <PageHeader
          title={t('title')}
          description={t('description')}
        />

        {error && <ErrorBanner message={error} />}

        {/* Filters */}
        <form onSubmit={handleApplyFilters} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6">
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
            <div>
              <label htmlFor="f-actor" className="block text-xs font-medium text-slate-400 mb-1">{t('filters.actorEmail')}</label>
              <input
                id="f-actor"
                type="text"
                value={filterActor}
                onChange={(e) => setFilterActor(e.target.value)}
                placeholder={t('filters.actorPlaceholder')}
                className="w-full px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>
            <div>
              <label htmlFor="f-resource" className="block text-xs font-medium text-slate-400 mb-1">{t('filters.resourceType')}</label>
              <input
                id="f-resource"
                type="text"
                value={filterResourceType}
                onChange={(e) => setFilterResourceType(e.target.value)}
                placeholder={t('filters.resourceTypePlaceholder')}
                className="w-full px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>
            <div>
              <label htmlFor="f-action" className="block text-xs font-medium text-slate-400 mb-1">{t('filters.method')}</label>
              <select
                id="f-action"
                value={filterAction}
                onChange={(e) => setFilterAction(e.target.value)}
                className="w-full px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              >
                <option value="">{t('filters.methodAll')}</option>
                <option value="GET">GET</option>
                <option value="POST">POST</option>
                <option value="PATCH">PATCH</option>
                <option value="PUT">PUT</option>
                <option value="DELETE">DELETE</option>
              </select>
            </div>
            <div>
              <label htmlFor="f-since" className="block text-xs font-medium text-slate-400 mb-1">{t('filters.since')}</label>
              <input
                id="f-since"
                type="datetime-local"
                value={filterSince}
                onChange={(e) => setFilterSince(e.target.value)}
                className="w-full px-3 py-1.5 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 text-sm focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
              />
            </div>
            <div>
              <label htmlFor="f-until" className="block text-xs font-medium text-slate-400 mb-1">{t('filters.until')}</label>
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
              {t('filters.clear')}
            </button>
            <button
              type="submit"
              className="px-4 py-1.5 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors"
            >
              {t('filters.apply')}
            </button>
          </div>
        </form>

        {loading ? (
          <LoadingSpinner />
        ) : entries.length === 0 ? (
          <EmptyState message={t('empty')} />
        ) : (
          <>
            <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <SortableHeader label={t('columns.timestamp')} sortKey="timestamp" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label={t('columns.actor')} sortKey="actor" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label={t('columns.action')} sortKey="action" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label={t('columns.resourceType')} sortKey="resourceType" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label={t('columns.resourceId')} sortKey="resourceId" sortState={sortState} onSort={toggleSort} className="hidden lg:table-cell" />
                      <SortableHeader label={t('columns.status')} sortKey="status" sortState={sortState} onSort={toggleSort} />
                      <SortableHeader label={t('columns.duration')} sortKey="duration" sortState={sortState} onSort={toggleSort} className="hidden md:table-cell" />
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
                  {t('pagination.summary', {
                    current: pagination['current-page'],
                    total: pagination['total-pages'],
                    count: pagination['total-count'],
                  })}
                </span>
                <div className="flex gap-2">
                  <button
                    onClick={() => setPage(Math.max(1, page - 1))}
                    disabled={page <= 1}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 disabled:bg-slate-800 disabled:text-slate-600 text-slate-200 transition-colors"
                  >
                    {t('pagination.previous')}
                  </button>
                  <button
                    onClick={() => setPage(page + 1)}
                    disabled={page >= pagination['total-pages']}
                    className="px-3 py-1.5 rounded-lg text-sm font-medium bg-slate-700 hover:bg-slate-600 disabled:bg-slate-800 disabled:text-slate-600 text-slate-200 transition-colors"
                  >
                    {t('pagination.next')}
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
