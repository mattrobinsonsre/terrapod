'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState, isAdmin } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface AgentPool {
  id: string
  attributes: {
    name: string
    description: string
    'service-account-name': string
    'is-default': boolean
    'created-at': string
  }
}

export default function AgentPoolsPage() {
  const router = useRouter()
  const [pools, setPools] = useState<AgentPool[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [serviceAccount, setServiceAccount] = useState('')
  const [creating, setCreating] = useState(false)

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    if (!isAdmin()) { router.push('/'); return }
    loadPools()
  }, [router])

  async function loadPools() {
    setLoading(true)
    try {
      const res = await apiFetch('/api/v2/organizations/default/agent-pools')
      if (!res.ok) throw new Error('Failed to load agent pools')
      const data = await res.json()
      setPools(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load agent pools')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const attrs: Record<string, unknown> = { name }
      if (description) attrs.description = description
      if (serviceAccount) attrs['service-account-name'] = serviceAccount

      const res = await apiFetch('/api/v2/organizations/default/agent-pools', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({ data: { type: 'agent-pools', attributes: attrs } }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create pool (${res.status})`)
      }
      setName('')
      setDescription('')
      setServiceAccount('')
      setShowCreate(false)
      await loadPools()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create pool')
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Agent Pools"
          description="Manage runner agent pools and their listeners"
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'New Pool'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div>
                <label htmlFor="pool-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input id="pool-name" type="text" value={name} onChange={(e) => setName(e.target.value)} required
                  placeholder="aws-prod"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="pool-desc" className="block text-sm font-medium text-slate-300 mb-1">Description</label>
                <input id="pool-desc" type="text" value={description} onChange={(e) => setDescription(e.target.value)}
                  placeholder="Production AWS runners"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
              <div>
                <label htmlFor="pool-sa" className="block text-sm font-medium text-slate-300 mb-1">Service Account</label>
                <input id="pool-sa" type="text" value={serviceAccount} onChange={(e) => setServiceAccount(e.target.value)}
                  placeholder="terrapod-runner"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent" />
              </div>
            </div>
            <button type="submit" disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors">
              {creating ? 'Creating...' : 'Create Pool'}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : pools.length === 0 ? (
          <EmptyState message="No agent pools configured." />
        ) : (
          <div className="bg-slate-800/50 rounded-lg border border-slate-700/50 overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-700/50">
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider">Name</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden sm:table-cell">Description</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden md:table-cell">Service Account</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-slate-400 uppercase tracking-wider hidden lg:table-cell">Created</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700/30">
                {pools.map((pool) => (
                  <tr key={pool.id} className="hover:bg-slate-700/20 transition-colors cursor-pointer"
                    onClick={() => router.push(`/admin/agent-pools/${pool.id}`)}>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <Link href={`/admin/agent-pools/${pool.id}`} className="text-sm font-medium text-brand-400 hover:text-brand-300"
                          onClick={(e) => e.stopPropagation()}>
                          {pool.attributes.name}
                        </Link>
                        {pool.attributes['is-default'] && (
                          <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-brand-900/50 text-brand-300">default</span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-sm text-slate-400 hidden sm:table-cell">
                      {pool.attributes.description || '-'}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-400 font-mono hidden md:table-cell">
                      {pool.attributes['service-account-name'] || '-'}
                    </td>
                    <td className="px-4 py-3 text-xs text-slate-500 hidden lg:table-cell">
                      {pool.attributes['created-at'] ? new Date(pool.attributes['created-at']).toLocaleDateString() : ''}
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
