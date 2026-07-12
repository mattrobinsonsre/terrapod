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
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { useSortable } from '@/lib/use-sortable'
import { useFormat } from '@/lib/format'

interface Session {
  email: string
  roles: string[]
  provider_name: string
  created_at: string
  expires_at: string
  last_active_at: string
  token_hint: string
  is_current: boolean
}

export default function SessionsPage() {
  const router = useRouter()
  const t = useTranslations('settings')
  const fmt = useFormat()
  const [sessions, setSessions] = useState<Session[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [admin, setAdmin] = useState(false)

  type SessionSortKey = 'provider' | 'created_at' | 'expires_at' | 'last_active_at'
  const { sortedItems: sortedSessions, sortState, toggleSort } = useSortable<Session, SessionSortKey>(
    sessions, 'created_at', 'desc',
    useCallback((item: Session, key: SessionSortKey) => {
      switch (key) {
        case 'provider': return item.provider_name
        case 'created_at': return item.created_at
        case 'expires_at': return item.expires_at
        case 'last_active_at': return item.last_active_at
      }
    }, []),
  )

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    const isAdminUser = isAdmin()
    setAdmin(isAdminUser)
    loadSessions(isAdminUser)
  }, [router])

  async function loadSessions(adminView: boolean) {
    try {
      const endpoint = adminView ? '/api/terrapod/v1/auth/sessions/all' : '/api/terrapod/v1/auth/sessions'
      const res = await apiFetch(endpoint)
      if (!res.ok) throw new Error(t('sessions.errors.load'))
      const data = await res.json()
      setSessions(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('sessions.errors.load'))
    } finally {
      setLoading(false)
    }
  }

  async function handleRevokeUser(email: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/terrapod/v1/auth/sessions/user/${encodeURIComponent(email)}`, {
        method: 'DELETE',
      })
      if (!res.ok && res.status !== 204) throw new Error(t('sessions.errors.revoke', { status: res.status }))
      await loadSessions(admin)
    } catch (err) {
      setError(err instanceof Error ? err.message : t('sessions.errors.revokeGeneric'))
    }
  }

  // Group sessions by email for admin view (sorting is applied before grouping)
  const grouped = admin
    ? sortedSessions.reduce<Record<string, Session[]>>((acc, s) => {
        ;(acc[s.email] ||= []).push(s)
        return acc
      }, {})
    : { '': sortedSessions }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title={t('sessions.title')}
          description={admin ? t('sessions.descriptionAll') : t('sessions.description')}
        />

        {error && <ErrorBanner message={error} />}

        {loading ? (
          <LoadingSpinner />
        ) : sessions.length === 0 ? (
          <EmptyState message={t('sessions.empty')} />
        ) : (
          <div className="space-y-6">
            {Object.entries(grouped).map(([email, userSessions]) => (
              <div key={email || 'self'}>
                {admin && email && (
                  <div className="flex items-center justify-between mb-2">
                    <h2 className="text-sm font-semibold text-slate-300">{email}</h2>
                    <button
                      onClick={() => handleRevokeUser(email)}
                      className="text-xs text-red-400 hover:text-red-300 transition-colors"
                    >
                      {t('sessions.revokeAll')}
                    </button>
                  </div>
                )}
                <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-slate-700/50">
                        <SortableHeader label={t('sessions.columns.provider')} sortKey="provider" sortState={sortState} onSort={toggleSort} />
                        <SortableHeader label={t('sessions.columns.created')} sortKey="created_at" sortState={sortState} onSort={toggleSort} />
                        <SortableHeader label={t('sessions.columns.expires')} sortKey="expires_at" sortState={sortState} onSort={toggleSort} />
                        <SortableHeader label={t('sessions.columns.lastActive')} sortKey="last_active_at" sortState={sortState} onSort={toggleSort} />
                        <th className="text-left px-4 py-3 text-slate-400 font-medium">{t('sessions.columns.token')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {userSessions.map((s) => (
                        <tr key={s.token_hint} className="border-b border-slate-700/30 last:border-0">
                          <td className="px-4 py-3 text-slate-200">
                            {s.provider_name}
                            {s.is_current && (
                              <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-brand-900/50 text-brand-300 border border-brand-700/50">
                                {t('sessions.current')}
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-3 text-slate-400 text-xs">{fmt.dateTime(s.created_at)}</td>
                          <td className="px-4 py-3 text-slate-400 text-xs">{fmt.dateTime(s.expires_at)}</td>
                          <td className="px-4 py-3 text-slate-400 text-xs">{fmt.dateTime(s.last_active_at)}</td>
                          <td className="px-4 py-3 text-slate-500 font-mono text-xs">...{s.token_hint}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </div>
        )}
      </main>
    </>
  )
}
