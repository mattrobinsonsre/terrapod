'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { getAuthState, getUserId } from '@/lib/auth'
import { apiFetch } from '@/lib/api'

interface Provider {
  id: string
  attributes: {
    name: string
    namespace: string
    'created-at': string | null
  }
}

export default function ProvidersPage() {
  const router = useRouter()
  const [providers, setProviders] = useState<Provider[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newNamespace, setNewNamespace] = useState('')
  const [creating, setCreating] = useState(false)

  const org = getUserId()

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadProviders()
  }, [router])

  async function loadProviders() {
    setLoading(true)
    try {
      const res = await apiFetch(`/api/v2/organizations/${org}/registry-providers`)
      if (!res.ok) throw new Error('Failed to load providers')
      const data = await res.json()
      setProviders(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load providers')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v2/organizations/${org}/registry-providers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'registry-providers',
            attributes: { name: newName, namespace: newNamespace || org },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create provider (${res.status})`)
      }
      setNewName('')
      setNewNamespace('')
      setShowCreate(false)
      await loadProviders()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create provider')
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Providers"
          description="Private Terraform provider registry"
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'Create Provider'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="prov-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input
                  id="prov-name"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                  placeholder="aws"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="prov-ns" className="block text-sm font-medium text-slate-300 mb-1">Namespace (optional)</label>
                <input
                  id="prov-ns"
                  type="text"
                  value={newNamespace}
                  onChange={(e) => setNewNamespace(e.target.value)}
                  placeholder={org}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
            </div>
            <button
              type="submit"
              disabled={creating}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
            >
              {creating ? 'Creating...' : 'Create'}
            </button>
          </form>
        )}

        {loading ? (
          <LoadingSpinner />
        ) : providers.length === 0 ? (
          <EmptyState message="No providers yet. Create one to get started." />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {providers.map((prov) => (
              <Link
                key={prov.id}
                href={`/registry/providers/${org}/${prov.attributes.namespace}/${prov.attributes.name}`}
                className="bg-slate-800/50 rounded-lg border border-slate-700/50 hover:border-brand-600/30 p-4 transition-colors"
              >
                <h3 className="font-semibold text-slate-200">{prov.attributes.name}</h3>
                <p className="text-sm text-slate-500 mt-1">Namespace: {prov.attributes.namespace}</p>
              </Link>
            ))}
          </div>
        )}
      </main>
    </>
  )
}
