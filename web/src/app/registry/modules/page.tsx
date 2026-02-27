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

interface Module {
  id: string
  attributes: {
    name: string
    namespace: string
    provider: string
    status: string
    'version-statuses': { version: string; status: string }[]
    'created-at': string | null
  }
}

export default function ModulesPage() {
  const router = useRouter()
  const [modules, setModules] = useState<Module[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Create form
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [newProvider, setNewProvider] = useState('')
  const [creating, setCreating] = useState(false)

  const org = getUserId()

  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadModules()
  }, [router])

  async function loadModules() {
    setLoading(true)
    try {
      const res = await apiFetch(`/api/v2/organizations/${org}/registry-modules`)
      if (!res.ok) throw new Error('Failed to load modules')
      const data = await res.json()
      setModules(data.data || [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load modules')
    } finally {
      setLoading(false)
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    setCreating(true)
    setError('')
    try {
      const res = await apiFetch(`/api/v2/organizations/${org}/registry-modules`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: {
            type: 'registry-modules',
            attributes: { name: newName, provider: newProvider },
          },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create module (${res.status})`)
      }
      setNewName('')
      setNewProvider('')
      setShowCreate(false)
      await loadModules()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create module')
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <NavBar />
      <main className="px-4 sm:px-6 lg:px-8 py-8 max-w-6xl mx-auto">
        <PageHeader
          title="Modules"
          description="Private Terraform module registry"
          actions={
            <button
              onClick={() => setShowCreate(!showCreate)}
              className="px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 text-white transition-colors btn-smoke"
            >
              {showCreate ? 'Cancel' : 'Create Module'}
            </button>
          }
        />

        {error && <ErrorBanner message={error} />}

        {showCreate && (
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="mod-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input
                  id="mod-name"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                  placeholder="vpc"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
              <div>
                <label htmlFor="mod-provider" className="block text-sm font-medium text-slate-300 mb-1">Provider</label>
                <input
                  id="mod-provider"
                  type="text"
                  value={newProvider}
                  onChange={(e) => setNewProvider(e.target.value)}
                  required
                  placeholder="aws"
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
        ) : modules.length === 0 ? (
          <EmptyState message="No modules yet. Create one to get started." />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {modules.map((mod) => (
              <Link
                key={mod.id}
                href={`/registry/modules/${org}/${mod.attributes.namespace}/${mod.attributes.name}/${mod.attributes.provider}`}
                className="bg-slate-800/50 rounded-lg border border-slate-700/50 hover:border-brand-600/30 p-4 transition-colors"
              >
                <h3 className="font-semibold text-slate-200">{mod.attributes.name}</h3>
                <p className="text-sm text-slate-500 mt-1">Provider: {mod.attributes.provider}</p>
                <div className="flex items-center gap-2 mt-2">
                  <span className="text-xs text-slate-400">
                    {mod.attributes['version-statuses']?.length || 0} version(s)
                  </span>
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                    mod.attributes.status === 'published'
                      ? 'bg-green-900/50 text-green-300'
                      : 'bg-slate-700 text-slate-400'
                  }`}>
                    {mod.attributes.status}
                  </span>
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>
    </>
  )
}
