'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import NavBar from '@/components/nav-bar'
import { PageHeader } from '@/components/page-header'
import { LoadingSpinner } from '@/components/loading-spinner'
import { ErrorBanner } from '@/components/error-banner'
import { EmptyState } from '@/components/empty-state'
import { LabelsEditor } from '@/components/labels-editor'
import { getAuthState } from '@/lib/auth'
import { apiFetch } from '@/lib/api'
import { usePollingInterval } from '@/lib/use-polling-interval'

interface VCSConnection {
  id: string
  attributes: { name: string; provider: string }
}

interface Module {
  id: string
  attributes: {
    name: string
    namespace: string
    provider: string
    status: string
    source: string
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
  const [newLabels, setNewLabels] = useState<Record<string, string>>({})
  const [newVcsConnectionId, setNewVcsConnectionId] = useState('')
  const [newVcsRepoUrl, setNewVcsRepoUrl] = useState('')
  const [newVcsBranch, setNewVcsBranch] = useState('')
  const [newVcsTagPattern, setNewVcsTagPattern] = useState('v*')
  const [vcsConnections, setVcsConnections] = useState<VCSConnection[]>([])
  const [creating, setCreating] = useState(false)



  useEffect(() => {
    if (!getAuthState()) { router.push('/login'); return }
    loadModules()
    loadVcsConnections()
  }, [router])

  usePollingInterval(!loading, 60_000, loadModules)

  async function loadVcsConnections() {
    try {
      const res = await apiFetch('/api/terrapod/v1/vcs-connections')
      if (res.ok) {
        const data = await res.json()
        setVcsConnections(data.data || [])
      }
    } catch {
      // VCS connections are optional
    }
  }

  async function loadModules() {
    try {
      const res = await apiFetch('/api/terrapod/v1/registry-modules')
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
      const attributes: Record<string, unknown> = {
        name: newName,
        provider: newProvider,
      }
      if (Object.keys(newLabels).length > 0) attributes.labels = newLabels
      if (newVcsConnectionId) {
        attributes['vcs-connection-id'] = newVcsConnectionId
        attributes['vcs-repo-url'] = newVcsRepoUrl
        attributes['vcs-branch'] = newVcsBranch
        attributes['vcs-tag-pattern'] = newVcsTagPattern
      }

      const res = await apiFetch('/api/terrapod/v1/registry-modules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/vnd.api+json' },
        body: JSON.stringify({
          data: { type: 'registry-modules', attributes },
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || `Failed to create module (${res.status})`)
      }
      setNewName('')
      setNewProvider('')
      setNewLabels({})
      setNewVcsConnectionId('')
      setNewVcsRepoUrl('')
      setNewVcsBranch('')
      setNewVcsTagPattern('v*')
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
          <form onSubmit={handleCreate} className="bg-slate-800/50 rounded-lg border border-slate-700/50 p-4 mb-6 space-y-4">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="mod-name" className="block text-sm font-medium text-slate-300 mb-1">Name</label>
                <input
                  id="mod-name"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  required
                  pattern="[a-z][a-z0-9-]*"
                  title="Lowercase letters, numbers, and hyphens only. Must start with a lowercase letter."
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
                  pattern="[a-z][a-z0-9-]*"
                  title="Lowercase letters, numbers, and hyphens only. Must start with a lowercase letter."
                  placeholder="aws"
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                />
              </div>
            </div>
            <p className="mt-1 text-xs text-slate-500">Name and provider form the module&apos;s registry address and cannot be changed after creation.</p>

            {/* VCS Configuration */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div>
                <label htmlFor="create-vcs-conn" className="block text-sm font-medium text-slate-300 mb-1">VCS Connection (optional)</label>
                <select
                  id="create-vcs-conn"
                  value={newVcsConnectionId}
                  onChange={(e) => setNewVcsConnectionId(e.target.value)}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
                >
                  <option value="">None — upload versions manually</option>
                  {vcsConnections.map((conn) => (
                    <option key={conn.id} value={conn.id}>
                      {conn.attributes.name} ({conn.attributes.provider})
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="create-vcs-repo" className="block text-sm font-medium text-slate-300 mb-1">Repository URL</label>
                <input
                  id="create-vcs-repo"
                  type="text"
                  value={newVcsRepoUrl}
                  onChange={(e) => setNewVcsRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/terraform-module-vpc"
                  disabled={!newVcsConnectionId}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent disabled:opacity-50"
                />
              </div>
              <div>
                <label htmlFor="create-vcs-branch" className="block text-sm font-medium text-slate-300 mb-1">Branch (optional)</label>
                <input
                  id="create-vcs-branch"
                  type="text"
                  value={newVcsBranch}
                  onChange={(e) => setNewVcsBranch(e.target.value)}
                  placeholder="main (leave empty for default)"
                  disabled={!newVcsConnectionId}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent disabled:opacity-50"
                />
              </div>
              <div>
                <label htmlFor="create-vcs-tag" className="block text-sm font-medium text-slate-300 mb-1">Tag Pattern</label>
                <input
                  id="create-vcs-tag"
                  type="text"
                  value={newVcsTagPattern}
                  onChange={(e) => setNewVcsTagPattern(e.target.value)}
                  placeholder="v*"
                  disabled={!newVcsConnectionId}
                  className="w-full px-3 py-2 border border-slate-600 rounded-lg bg-slate-700 text-slate-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent disabled:opacity-50"
                />
                <p className="mt-1 text-xs text-slate-500">Only tags matching this pattern create versions (e.g. v1.0.0)</p>
              </div>
            </div>

            {/* Labels */}
            <div className="pt-2">
              <label className="block text-sm font-medium text-slate-300 mb-1">Labels (optional)</label>
              <LabelsEditor labels={newLabels} onChange={setNewLabels} />
            </div>

            <button
              type="submit"
              disabled={creating}
              className="mt-2 px-4 py-2 rounded-lg text-sm font-medium bg-brand-600 hover:bg-brand-500 disabled:bg-brand-800 disabled:text-brand-400 text-white transition-colors"
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
                href={`/registry/modules/${mod.attributes.name}/${mod.attributes.provider}`}
                className="bg-slate-800/50 rounded-lg border border-slate-700/50 hover:border-brand-600/30 p-4 transition-colors"
              >
                <h3 className="font-semibold text-slate-200">{mod.attributes.name}</h3>
                <p className="text-sm text-slate-500 mt-1">Provider: {mod.attributes.provider}</p>
                <div className="flex items-center gap-2 mt-2">
                  <span className="text-xs text-slate-400">
                    {mod.attributes['version-statuses']?.length || 0} version(s)
                  </span>
                  <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${
                    mod.attributes.status === 'setup_complete'
                      ? 'bg-green-900/50 text-green-300'
                      : 'bg-slate-700 text-slate-400'
                  }`}>
                    {mod.attributes.status}
                  </span>
                  {mod.attributes.source === 'vcs' && (
                    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-900/50 text-blue-300">
                      VCS
                    </span>
                  )}
                </div>
              </Link>
            ))}
          </div>
        )}
      </main>
    </>
  )
}
