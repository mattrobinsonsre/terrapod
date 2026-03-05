'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface WorkspaceHealth {
  total: number
  locked: number
  'drift-enabled': number
  'by-drift-status': Record<string, number>
  stale: StaleWorkspace[]
}

interface StaleWorkspace {
  id: string
  name: string
  'last-applied-at': string
  'days-since-apply': number
  'drift-status': string
}

interface RunHealth {
  queued: number
  'in-progress': number
  'recent-24h': {
    total: number
    applied: number
    errored: number
    canceled: number
  }
  'average-plan-duration-seconds': number
  'average-apply-duration-seconds': number
}

interface ListenerDetail {
  id: string
  name: string
  'pool-name': string
  status: string
  capacity: number
  'active-runs': number
  'last-heartbeat': string
}

interface ListenerHealth {
  total: number
  online: number
  offline: number
  details: ListenerDetail[]
}

interface DashboardData {
  workspaces: WorkspaceHealth
  runs: RunHealth
  listeners: ListenerHealth
}

export default function HealthDashboardPage() {
  const router = useRouter()
  const [data, setData] = useState<DashboardData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const auth = getAuthState()
    if (!auth) { router.push('/login'); return }
    if (!isAdmin()) { setError('Admin access required'); setLoading(false); return }
    loadDashboard()
  }, [router])

  async function loadDashboard() {
    try {
      const res = await apiFetch('/api/v2/admin/health-dashboard')
      if (!res.ok) {
        if (res.status === 403) throw new Error('Admin or audit role required')
        throw new Error('Failed to load health dashboard')
      }
      const json = await res.json()
      setData(json.data.attributes)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load health dashboard')
    } finally {
      setLoading(false)
    }
  }

  function driftStatusColor(s: string): string {
    switch (s) {
      case 'no-drift': return 'text-green-400'
      case 'drifted': return 'text-amber-400'
      case 'errored': return 'text-red-400'
      default: return 'text-slate-400'
    }
  }

  function formatDuration(seconds: number): string {
    if (seconds === 0) return '-'
    if (seconds < 60) return `${seconds}s`
    const min = Math.floor(seconds / 60)
    const sec = seconds % 60
    return sec > 0 ? `${min}m ${sec}s` : `${min}m`
  }

  if (loading) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><LoadingSpinner /></main></>
  if (error) return <><NavBar /><main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto"><ErrorBanner message={error} /></main></>
  if (!data) return null

  const ws = data.workspaces
  const runs = data.runs
  const listeners = data.listeners

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Health Dashboard"
          description="Platform health at a glance"
        />

        <div className="space-y-6">
          {/* Workspaces */}
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
            <h3 className="text-sm font-medium text-slate-300 mb-4">Workspaces</h3>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-4">
              <div>
                <div className="text-2xl font-semibold text-slate-100">{ws.total}</div>
                <div className="text-xs text-slate-500">Total</div>
              </div>
              <div>
                <div className="text-2xl font-semibold text-amber-400">{ws.locked}</div>
                <div className="text-xs text-slate-500">Locked</div>
              </div>
              <div>
                <div className="text-2xl font-semibold text-brand-400">{ws['drift-enabled']}</div>
                <div className="text-xs text-slate-500">Drift Enabled</div>
              </div>
              <div className="flex flex-wrap gap-2">
                {Object.entries(ws['by-drift-status']).map(([status, count]) => (
                  <div key={status} className="text-center">
                    <div className={`text-lg font-semibold ${driftStatusColor(status)}`}>{count}</div>
                    <div className="text-xs text-slate-500">{status}</div>
                  </div>
                ))}
              </div>
            </div>

            {ws.stale.length > 0 && (
              <>
                <h4 className="text-xs font-medium text-slate-400 mb-2 mt-4">Most Stale Workspaces</h4>
                <div className="overflow-hidden rounded border border-slate-700/50">
                  <table className="w-full">
                    <thead>
                      <tr className="border-b border-slate-700/50">
                        <th className="px-3 py-2 text-left text-xs font-medium text-slate-400">Name</th>
                        <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 hidden sm:table-cell">Last Applied</th>
                        <th className="px-3 py-2 text-left text-xs font-medium text-slate-400">Days</th>
                        <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 hidden sm:table-cell">Drift</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-700/30">
                      {ws.stale.map((s) => (
                        <tr key={s.id} className="hover:bg-slate-700/20 transition-colors">
                          <td className="px-3 py-2 text-sm text-brand-400 font-medium">
                            <a href={`/workspaces/${s.id}`} className="hover:text-brand-300">{s.name}</a>
                          </td>
                          <td className="px-3 py-2 text-xs text-slate-400 hidden sm:table-cell">
                            {s['last-applied-at'] || 'Never'}
                          </td>
                          <td className="px-3 py-2 text-xs text-slate-300">
                            {s['days-since-apply'] === -1 ? 'Never' : s['days-since-apply']}
                          </td>
                          <td className={`px-3 py-2 text-xs hidden sm:table-cell ${driftStatusColor(s['drift-status'])}`}>
                            {s['drift-status']}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>

          {/* Runs (24h) */}
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
            <h3 className="text-sm font-medium text-slate-300 mb-4">Runs</h3>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 mb-4">
              <div>
                <div className="text-2xl font-semibold text-yellow-400">{runs.queued}</div>
                <div className="text-xs text-slate-500">Queued</div>
              </div>
              <div>
                <div className="text-2xl font-semibold text-blue-400">{runs['in-progress']}</div>
                <div className="text-xs text-slate-500">In Progress</div>
              </div>
              <div>
                <div className="text-2xl font-semibold text-slate-100">{formatDuration(runs['average-plan-duration-seconds'])}</div>
                <div className="text-xs text-slate-500">Avg Plan</div>
              </div>
              <div>
                <div className="text-2xl font-semibold text-slate-100">{formatDuration(runs['average-apply-duration-seconds'])}</div>
                <div className="text-xs text-slate-500">Avg Apply</div>
              </div>
            </div>
            <h4 className="text-xs font-medium text-slate-400 mb-2">Last 24 Hours</h4>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              <div>
                <div className="text-lg font-semibold text-slate-100">{runs['recent-24h'].total}</div>
                <div className="text-xs text-slate-500">Total</div>
              </div>
              <div>
                <div className="text-lg font-semibold text-green-400">{runs['recent-24h'].applied}</div>
                <div className="text-xs text-slate-500">Applied</div>
              </div>
              <div>
                <div className="text-lg font-semibold text-red-400">{runs['recent-24h'].errored}</div>
                <div className="text-xs text-slate-500">Errored</div>
              </div>
              <div>
                <div className="text-lg font-semibold text-slate-400">{runs['recent-24h'].canceled}</div>
                <div className="text-xs text-slate-500">Canceled</div>
              </div>
            </div>
          </div>

          {/* Listeners */}
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-6">
            <h3 className="text-sm font-medium text-slate-300 mb-4">Listeners</h3>
            <div className="grid grid-cols-3 gap-4 mb-4">
              <div>
                <div className="text-2xl font-semibold text-slate-100">{listeners.total}</div>
                <div className="text-xs text-slate-500">Total</div>
              </div>
              <div>
                <div className="text-2xl font-semibold text-green-400">{listeners.online}</div>
                <div className="text-xs text-slate-500">Online</div>
              </div>
              <div>
                <div className="text-2xl font-semibold text-red-400">{listeners.offline}</div>
                <div className="text-xs text-slate-500">Offline</div>
              </div>
            </div>

            {listeners.details.length > 0 && (
              <div className="overflow-hidden rounded border border-slate-700/50">
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-slate-700/50">
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400">Name</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 hidden sm:table-cell">Pool</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400">Status</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 hidden sm:table-cell">Capacity</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 hidden sm:table-cell">Active</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-slate-400 hidden md:table-cell">Heartbeat</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-700/30">
                    {listeners.details.map((l) => (
                      <tr key={l.id} className="hover:bg-slate-700/20 transition-colors">
                        <td className="px-3 py-2 text-sm text-slate-200">{l.name}</td>
                        <td className="px-3 py-2 text-xs text-slate-400 hidden sm:table-cell">{l['pool-name'] || '-'}</td>
                        <td className="px-3 py-2">
                          <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                            l.status === 'online' ? 'bg-green-900/50 text-green-300' : 'bg-red-900/50 text-red-300'
                          }`}>
                            {l.status}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-xs text-slate-300 hidden sm:table-cell">{l.capacity}</td>
                        <td className="px-3 py-2 text-xs text-slate-300 hidden sm:table-cell">{l['active-runs']}</td>
                        <td className="px-3 py-2 text-xs text-slate-500 hidden md:table-cell">
                          {l['last-heartbeat'] ? new Date(l['last-heartbeat']).toLocaleString() : '-'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      </main>
    </>
  )
}
