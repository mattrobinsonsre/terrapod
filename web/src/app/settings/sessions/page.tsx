'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

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
  const [sessions, setSessions] = useState<Session[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [admin, setAdmin] = useState(false)

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    const isAdminUser = isAdmin()
    setAdmin(isAdminUser)
    loadSessions(isAdminUser)
  }, [router])

  async function loadSessions(adminView: boolean) {
    setLoading(true)
    try {
      const endpoint = adminView ? '/api/v2/auth/sessions/all' : '/api/v2/auth/sessions'
      const res = await apiFetch(endpoint)
      if (!res.ok) throw new Error('Failed to load sessions')
      const data = await res.json()
      setSessions(data)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load sessions')
    } finally {
      setLoading(false)
    }
  }

  async function handleRevokeUser(email: string) {
    setError('')
    try {
      const res = await apiFetch(`/api/v2/auth/sessions/user/${encodeURIComponent(email)}`, {
        method: 'DELETE',
      })
      if (!res.ok && res.status !== 204) throw new Error(`Failed to revoke sessions (${res.status})`)
      await loadSessions(admin)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke sessions')
    }
  }

  function formatDate(iso: string): string {
    return new Date(iso).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  }

  // Group sessions by email for admin view
  const grouped = admin
    ? sessions.reduce<Record<string, Session[]>>((acc, s) => {
        ;(acc[s.email] ||= []).push(s)
        return acc
      }, {})
    : { '': sessions }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Sessions"
          description={admin ? 'All active sessions across the platform' : 'Your active sessions'}
        />

        {error && <ErrorBanner message={error} />}

        {loading ? (
          <LoadingSpinner />
        ) : sessions.length === 0 ? (
          <EmptyState message="No active sessions." />
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
                      Revoke All
                    </button>
                  </div>
                )}
                <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-slate-700/50">
                        <th className="text-left px-4 py-3 text-slate-400 font-medium">Provider</th>
                        <th className="text-left px-4 py-3 text-slate-400 font-medium">Created</th>
                        <th className="text-left px-4 py-3 text-slate-400 font-medium">Expires</th>
                        <th className="text-left px-4 py-3 text-slate-400 font-medium">Last Active</th>
                        <th className="text-left px-4 py-3 text-slate-400 font-medium">Token</th>
                      </tr>
                    </thead>
                    <tbody>
                      {userSessions.map((s, i) => (
                        <tr key={i} className="border-b border-slate-700/30 last:border-0">
                          <td className="px-4 py-3 text-slate-200">
                            {s.provider_name}
                            {s.is_current && (
                              <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-brand-900/50 text-brand-300 border border-brand-700/50">
                                Current
                              </span>
                            )}
                          </td>
                          <td className="px-4 py-3 text-slate-400 text-xs">{formatDate(s.created_at)}</td>
                          <td className="px-4 py-3 text-slate-400 text-xs">{formatDate(s.expires_at)}</td>
                          <td className="px-4 py-3 text-slate-400 text-xs">{formatDate(s.last_active_at)}</td>
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
