'use client'

import { useCallback, useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { SortableHeader } from '@/components/sortable-header'
import { useIsAdmin } from '@/lib/use-auth-roles'
import { apiFetch, fetchAllPages } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'

interface DeletedWorkspace {
  id: string
  attributes: {
    'workspace-id': string
    'workspace-name': string | null
    'deleted-at': string
    'deleted-by': string | null
    'marker-reason': string
    'last-serial': number | null
    lineage: string | null
    'state-versions-available': number
    'age-days': number | null
    'restorable-until': string | null
    // Workspaces this deletion has already been restored into. Not
    // bookkeeping: a second restore puts a second live workspace with the
    // same state lineage over the same infrastructure, so this is what the
    // operator needs to see BEFORE clicking Restore (#1299).
    'restored-to': string[]
    settings: Record<string, unknown>
    'variable-names': { key: string; category: string; sensitive: boolean }[]
  }
}

type SortKey = 'name' | 'deleted' | 'versions' | 'until'

function formatDate(raw: string | null): string {
  if (!raw) return '—'
  const d = new Date(raw)
  return Number.isNaN(d.getTime()) ? raw : d.toLocaleString()
}

export default function DeletedWorkspacesPage() {
  const t = useTranslations('adminDeletedWorkspaces')
  // Gated on the post-mount value rather than isAdmin() so the first render
  // matches SSR, which has no roles — the same hydration guard the other
  // admin pages use.
  const isAdminClient = useIsAdmin()

  const [items, setItems] = useState<DeletedWorkspace[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [restoring, setRestoring] = useState<string | null>(null)
  const [notice, setNotice] = useState('')

  const load = useCallback(async () => {
    try {
      const data = await fetchAllPages<DeletedWorkspace>(
        '/api/terrapod/v1/deleted-workspaces'
      )
      setItems(data)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const { sortedItems: sorted, sortState, toggleSort } = useSortable<DeletedWorkspace, SortKey>(
    items,
    'deleted',
    'desc',
    (w, key) => {
      switch (key) {
        case 'name':
          return w.attributes['workspace-name'] ?? ''
        case 'versions':
          return w.attributes['state-versions-available']
        case 'until':
          return w.attributes['restorable-until'] ?? ''
        default:
          return w.attributes['deleted-at'] ?? ''
      }
    }
  )

  async function restore(w: DeletedWorkspace) {
    const name = w.attributes['workspace-name'] || w.attributes['workspace-id']
    const prior = w.attributes['restored-to'] ?? []
    // Confirmed on BOTH pointer types, not touch only. Restore is not a
    // destructive action, but it is not an undo either — the dialog spells
    // out what the operator actually gets, so a restore is never a casual
    // click that leaves them expecting the original workspace back.
    //
    // A repeat gets its own wording, because the risk is different in kind:
    // the outcome is two live workspaces over one set of infrastructure. The
    // server refuses by default, so confirming here is what sends force —
    // consent to that specific consequence, not a generic "are you sure".
    const confirmed = prior.length
      ? window.confirm(t('restoreAgainConfirm', { name, existing: prior.join(', ') }))
      : window.confirm(t('restoreConfirm', { name }))
    if (!confirmed) return

    setRestoring(w.id)
    setNotice('')
    try {
      const res = await apiFetch(
        `/api/terrapod/v1/deleted-workspaces/${w.attributes['workspace-id']}/restore`,
        {
          method: 'POST',
          // apiFetch does not set a content-type; without this the body
          // arrives as a raw string and the endpoint rejects it with 422.
          headers: { 'Content-Type': 'application/vnd.api+json' },
          body: JSON.stringify({
            data: { type: 'workspaces', attributes: prior.length ? { force: true } : {} },
          }),
        }
      )
      const body = await res.json()
      const attrs = body?.data?.attributes ?? {}
      setNotice(
        t('restoreDone', {
          name: attrs.name ?? name,
          versions: attrs['state-versions-restored'] ?? 0,
        })
      )
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRestoring(null)
    }
  }

  if (loading) return <LoadingSpinner />

  return (
    <>
      <NavBar />
      <main className="mx-auto max-w-6xl px-4 py-6">
        <PageHeader title={t('title')} description={t('description')} />

        {error && <ErrorBanner message={error} />}
        {notice && (
          <div className="mb-4 rounded-lg border border-green-800 bg-green-950/40 px-4 py-3 text-sm text-green-300">
            {notice}
          </div>
        )}

        {sorted.length === 0 ? (
          <EmptyState message={t('empty')} />
        ) : (
          <>
            {/* Desktop */}
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-700 text-left text-slate-400">
                    <SortableHeader
                      label={t('colName')}
                      sortKey="name"
                      sortState={sortState}
                      onSort={toggleSort}
                    />
                    <SortableHeader
                      label={t('colDeleted')}
                      sortKey="deleted"
                      sortState={sortState}
                      onSort={toggleSort}
                    />
                    <SortableHeader
                      label={t('colVersions')}
                      sortKey="versions"
                      sortState={sortState}
                      onSort={toggleSort}
                    />
                    <SortableHeader
                      label={t('colUntil')}
                      sortKey="until"
                      sortState={sortState}
                      onSort={toggleSort}
                    />
                    <th className="px-3 py-2">{t('colActions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((w) => (
                    <tr key={w.id} className="border-b border-slate-800">
                      <td className="px-3 py-2">
                        <div className="font-medium text-slate-100">
                          {w.attributes['workspace-name'] || t('unknownName')}
                        </div>
                        <div className="font-mono text-xs text-slate-500">
                          {w.attributes['workspace-id']}
                        </div>
                        {w.attributes['marker-reason'] === 'discovered-orphaned' && (
                          <span className="mt-1 inline-block rounded bg-amber-900/40 px-2 py-0.5 text-xs text-amber-300">
                            {t('orphanBadge')}
                          </span>
                        )}
                        {(w.attributes['restored-to'] ?? []).length > 0 && (
                          <span className="mt-1 ms-1 inline-block rounded bg-sky-900/40 px-2 py-0.5 text-xs text-sky-300">
                            {t('restoredBadge')}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-slate-300">
                        {formatDate(w.attributes['deleted-at'])}
                        {w.attributes['deleted-by'] && (
                          <div className="text-xs text-slate-500">
                            {w.attributes['deleted-by']}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2 tabular-nums text-slate-300">
                        {w.attributes['state-versions-available']}
                      </td>
                      <td className="px-3 py-2 text-slate-300">
                        {w.attributes['restorable-until']
                          ? formatDate(w.attributes['restorable-until'])
                          : t('retentionDisabled')}
                      </td>
                      <td className="px-3 py-2">
                        {isAdminClient && (
                          <button
                            onClick={() => void restore(w)}
                            disabled={restoring === w.id}
                            className="rounded-lg bg-slate-700 px-3 py-1.5 text-xs font-medium hover:bg-slate-600 disabled:opacity-50"
                          >
                            {restoring === w.id ? t('restoring') : t('restore')}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile — the same data as cards. Deletion date and the
                remaining window are the primary signal here and are never
                dropped; lineage and serial live on the desktop table only. */}
            <ul className="space-y-3 md:hidden">
              {sorted.map((w) => (
                <li
                  key={w.id}
                  className="rounded-lg border border-slate-800 bg-slate-900/50 p-4"
                >
                  <div className="font-medium text-slate-100">
                    {w.attributes['workspace-name'] || t('unknownName')}
                  </div>
                  <div className="mt-0.5 font-mono text-xs break-all text-slate-500">
                    {w.attributes['workspace-id']}
                  </div>
                  {w.attributes['marker-reason'] === 'discovered-orphaned' && (
                    <span className="mt-2 inline-block rounded bg-amber-900/40 px-2 py-0.5 text-xs text-amber-300">
                      {t('orphanBadge')}
                    </span>
                  )}
                  {(w.attributes['restored-to'] ?? []).length > 0 && (
                    <span className="mt-2 ms-1 inline-block rounded bg-sky-900/40 px-2 py-0.5 text-xs text-sky-300">
                      {t('restoredBadge')}
                    </span>
                  )}
                  <dl className="mt-3 space-y-1 text-sm">
                    <div className="flex justify-between gap-3">
                      <dt className="text-slate-400">{t('colDeleted')}</dt>
                      <dd className="text-end text-slate-300">
                        {formatDate(w.attributes['deleted-at'])}
                      </dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt className="text-slate-400">{t('colVersions')}</dt>
                      <dd className="tabular-nums text-slate-300">
                        {w.attributes['state-versions-available']}
                      </dd>
                    </div>
                    <div className="flex justify-between gap-3">
                      <dt className="text-slate-400">{t('colUntil')}</dt>
                      <dd className="text-end text-slate-300">
                        {w.attributes['restorable-until']
                          ? formatDate(w.attributes['restorable-until'])
                          : t('retentionDisabled')}
                      </dd>
                    </div>
                  </dl>
                  {isAdminClient && (
                    <button
                      onClick={() => void restore(w)}
                      disabled={restoring === w.id}
                      className="mt-3 w-full rounded-lg bg-slate-700 px-3 py-2 text-sm font-medium hover:bg-slate-600 disabled:opacity-50"
                    >
                      {restoring === w.id ? t('restoring') : t('restore')}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </main>
    </>
  )
}
